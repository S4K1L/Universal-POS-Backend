import uuid

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Ctx, tenant_ctx
from app.api.v1.auth import normalise_phone, revoke_sessions
from app.core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationFailed
from app.core.permissions import ASSIGNABLE_ROLES, Permission, ROLE_LABELS, ROLE_PERMISSIONS, UNGRANTABLE
from app.core.security import hash_secret
from app.db.session import get_db
from app.models.identity import User
from app.models.tenant import Branch
from app.schemas.common import Strict
from app.services import context as context_service
from app.services import subscription
from app.services.common import audit

router = APIRouter(prefix="/users", tags=["Staff"])
manage = tenant_ctx(permission=Permission.manage_staff, feature="staff", write=True)


class UserIn(Strict):
    name: str = Field(min_length=2, max_length=160)
    phone: str = Field(min_length=10, max_length=20)
    email: str = Field(default="", max_length=200)
    password: str = Field(min_length=8, max_length=72)
    role: str
    permissions: list[str] | None = None
    branch_ids: list[uuid.UUID] | None = None


class UserPatch(Strict):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    email: str | None = Field(default=None, max_length=200)
    role: str | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=72)
    permissions: list[str] | None = None
    clear_permissions: bool = False
    branch_ids: list[uuid.UUID] | None = None
    clear_branch_restriction: bool = False


async def _check_branches(db: AsyncSession, ctx: Ctx, ids: list[uuid.UUID] | None) -> list[str] | None:
    if ids is None:
        return None
    for bid in ids:
        b = await db.get(Branch, bid)
        if b is None or b.tenant_id != ctx.tenant.id:
            raise NotFoundError(f"Branch {bid} not found")
    return [str(i) for i in ids]


def _check_permissions(perms: list[str] | None) -> list[str] | None:
    if perms is None:
        return None
    valid = {p.value for p in Permission}
    bad = [p for p in perms if p not in valid]
    if bad:
        raise ValidationFailed(f"Unknown permission: {bad[0]}", code="validation_failed")
    withheld = [p for p in perms if Permission(p) in UNGRANTABLE]
    if withheld:
        raise ValidationFailed(f"{withheld[0]} rides with the owner role and can't be granted", code="validation_failed")
    return sorted(set(perms))


@router.get("", summary="Staff of this business")
async def list_users(ctx: Ctx = Depends(tenant_ctx(any_permission=(Permission.manage_staff, Permission.manage_orders, Permission.view_reports), allow_lapsed=True)),
                     db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(User).where(User.tenant_id == ctx.tenant.id).order_by(User.name))).scalars().all()
    return [context_service.user_out(u) for u in rows]


@router.get("/roles", summary="Roles and what each can do by default")
async def roles(ctx: Ctx = Depends(tenant_ctx(allow_lapsed=True))) -> dict:
    return {
        "roles": [{"key": r, "label": ROLE_LABELS[r], "permissions": sorted(p.value for p in ROLE_PERMISSIONS[r])} for r in ASSIGNABLE_ROLES],
        "permissions": [p.value for p in Permission if p not in UNGRANTABLE],
    }


@router.post("", status_code=201)
async def create_user(body: UserIn, ctx: Ctx = Depends(manage), db: AsyncSession = Depends(get_db)) -> dict:
    if body.role not in ASSIGNABLE_ROLES:
        raise ValidationFailed("Unknown role", code="validation_failed")
    if body.role == "owner" and ctx.user.role != "owner":
        raise PermissionDenied("Only an owner can create another owner")
    phone = normalise_phone(body.phone)
    if (await db.execute(select(User.id).where(User.phone == phone))).first():
        raise ConflictError("That phone number already has an account", code="phone_taken")
    await subscription.assert_within_limit(db, ctx.tenant, "staff")
    branch_ids = await _check_branches(db, ctx, body.branch_ids)
    if body.role == "owner" and branch_ids is not None:
        raise ValidationFailed("An owner can't be limited to branches", code="validation_failed")
    u = User(tenant_id=ctx.tenant.id, name=body.name.strip(), phone=phone, email=body.email, password_hash=hash_secret(body.password),
             role=body.role, permissions=_check_permissions(body.permissions), branch_ids=branch_ids)
    db.add(u)
    await db.flush()  # the id is assigned on flush; the audit row needs it
    await audit(db, action="staff.created", actor=ctx.user, tenant_id=ctx.tenant.id, entity="user", entity_id=u.id, summary=f"{u.name} ({u.role})")
    await db.commit()
    return context_service.user_out(u)


@router.patch("/{user_id}")
async def update_user(user_id: uuid.UUID, body: UserPatch, ctx: Ctx = Depends(manage), db: AsyncSession = Depends(get_db)) -> dict:
    u = await db.get(User, user_id)
    if u is None or u.tenant_id != ctx.tenant.id:
        raise NotFoundError("User not found")
    if u.role == "owner" and ctx.user.role != "owner":
        raise PermissionDenied("Only an owner can change an owner")
    if body.role:
        if body.role not in ASSIGNABLE_ROLES:
            raise ValidationFailed("Unknown role", code="validation_failed")
        if body.role == "owner" and ctx.user.role != "owner":
            raise PermissionDenied("Only an owner can promote to owner")
        if u.role == "owner" and body.role != "owner":
            others = (await db.execute(select(User.id).where(
                User.tenant_id == ctx.tenant.id, User.role == "owner", User.active.is_(True), User.id != u.id).limit(1))).first()
            if others is None:
                raise ConflictError("A business needs at least one owner", code="last_owner")
        u.role = body.role
        if body.role == "owner":
            u.branch_ids = None  # an owner always sees every branch
    if body.name:
        u.name = body.name.strip()
    if body.email is not None:
        u.email = body.email
    if body.active is not None:
        if not body.active and u.id == ctx.user.id:
            raise ConflictError("You can't deactivate your own account", code="self_deactivate")
        if body.active and not u.active:
            await subscription.assert_within_limit(db, ctx.tenant, "staff")
        u.active = body.active
    if body.password:
        u.password_hash = hash_secret(body.password)
        await revoke_sessions(db, u.id)
    if body.clear_permissions:
        u.permissions = None
    elif body.permissions is not None:
        u.permissions = _check_permissions(body.permissions)
    if body.clear_branch_restriction:
        u.branch_ids = None
    elif body.branch_ids is not None:
        if u.role == "owner":
            raise ValidationFailed("An owner can't be limited to branches", code="validation_failed")
        u.branch_ids = await _check_branches(db, ctx, body.branch_ids)
    await audit(db, action="staff.updated", actor=ctx.user, tenant_id=ctx.tenant.id, entity="user", entity_id=u.id,
                summary=f"{u.name}: " + ", ".join(sorted(body.model_dump(exclude_unset=True, exclude={'password'}))))
    await db.commit()
    return context_service.user_out(u)
