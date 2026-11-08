import re
import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_super_admin
from app.api.v1.auth import normalise_phone, revoke_sessions
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.permissions import ROLE_LABELS
from app.core.security import hash_secret
from app.db.session import get_db
from app.models.identity import User
from app.models.tenant import Tenant
from app.schemas.common import Strict
from app.services.common import audit

router = APIRouter(prefix="/admin", tags=["Platform Console — Businesses"], dependencies=[Depends(require_super_admin)])


class AdminIn(Strict):
    name: str = Field(min_length=2, max_length=160)
    phone: str = Field(min_length=6, max_length=20)
    password: str = Field(min_length=8, max_length=72)
    email: str = Field(default="", max_length=200)


def _user_row(u: User, tenant_name: str | None) -> dict:
    return {
        "id": str(u.id), "name": u.name, "phone": u.phone, "email": u.email, "role": u.role,
        "role_label": "Platform admin" if u.role == "super_admin" else ROLE_LABELS.get(u.role, u.role),
        "active": u.active, "tenant_id": str(u.tenant_id) if u.tenant_id else None, "tenant_name": tenant_name,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None, "created_at": u.created_at.isoformat(),
    }


async def _tenant_names(db: AsyncSession, users: list[User]) -> dict[uuid.UUID, str]:
    ids = {u.tenant_id for u in users if u.tenant_id}
    if not ids:
        return {}
    return {t.id: t.name for t in (await db.execute(select(Tenant).where(Tenant.id.in_(ids)))).scalars()}


@router.get("/users", summary="Every account on the platform: platform admins, business owners and their staff")
async def list_users(
    q: str = Query(default="", max_length=80), group: str = Query(default="all", pattern="^(all|admin|owner|staff)$"),
    status: str = Query(default="all", pattern="^(all|active|inactive)$"), tenant_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), db: AsyncSession = Depends(get_db),
) -> dict:
    conds = []
    if group == "admin":
        conds.append(User.role == "super_admin")
    elif group == "owner":
        conds.append(User.role == "owner")
    elif group == "staff":
        conds.append(User.role.notin_(("super_admin", "owner")))
    if status != "all":
        conds.append(User.active.is_(status == "active"))
    if tenant_id:
        conds.append(User.tenant_id == tenant_id)
    if q.strip():
        like = f"%{q.strip()}%"
        by_business = select(Tenant.id).where(Tenant.name.ilike(like))
        conds.append(or_(User.name.ilike(like), User.phone.ilike(like), User.email.ilike(like), User.tenant_id.in_(by_business)))
    total = (await db.execute(select(func.count()).select_from(User).where(*conds))).scalar_one()
    rows = (await db.execute(select(User).where(*conds).order_by(User.created_at.desc(), User.name).limit(limit).offset(offset))).scalars().all()
    names = await _tenant_names(db, list(rows))
    counts = dict((await db.execute(select(User.role, func.count()).group_by(User.role))).all())
    everyone = sum(counts.values())
    return {
        "items": [_user_row(u, names.get(u.tenant_id)) for u in rows], "total": int(total), "limit": limit, "offset": offset,
        "counts": {"all": int(everyone), "admin": int(counts.get("super_admin", 0)), "owner": int(counts.get("owner", 0)),
                   "staff": int(everyone - counts.get("super_admin", 0) - counts.get("owner", 0))},
    }


@router.get("/admins", summary="Platform admins, including ones that were removed")
async def list_admins(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(User).where(User.role == "super_admin").order_by(User.active.desc(), User.created_at))).scalars().all()
    return [_user_row(u, None) for u in rows]


@router.post("/admins", status_code=201, summary="Add a platform admin (or bring a removed one back)")
async def add_admin(body: AdminIn, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    phone = normalise_phone(body.phone)
    if not re.fullmatch(r"01[3-9]\d{8}", phone):
        raise ValidationFailed("Enter a Bangladesh mobile number like 01712345678")
    existing = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
    if existing is not None and not (existing.role == "super_admin" and not existing.active):
        raise ConflictError("That phone number already has an account", code="phone_taken")
    if existing is not None:  # a removed admin returns with the new name and password
        existing.name, existing.email, existing.password_hash, existing.active = body.name.strip(), body.email.strip(), hash_secret(body.password), True
        user, action = existing, "admin.restored"
    else:
        user = User(tenant_id=None, name=body.name.strip(), phone=phone, email=body.email.strip(), password_hash=hash_secret(body.password), role="super_admin")
        db.add(user)
        action = "admin.added"
    await db.flush()
    await audit(db, action=action, actor=admin, tenant_id=None, entity="user", entity_id=user.id, summary=f"{user.name} ({user.phone})")
    await db.commit()
    return _user_row(user, None)


@router.delete("/admins/{admin_id}", summary="Remove a platform admin: signed out at once; the record stays for the audit trail")
async def remove_admin(admin_id: uuid.UUID, me: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    target = await db.get(User, admin_id)
    if target is None or target.role != "super_admin":
        raise NotFoundError("Platform admin not found")
    if target.id == me.id:
        raise ConflictError("You cannot remove your own account — ask another admin to do it", code="cannot_remove_self")
    if target.active:
        remaining = (await db.execute(select(func.count()).select_from(User).where(User.role == "super_admin", User.active.is_(True), User.id != target.id))).scalar_one()
        if remaining < 1:
            raise ConflictError("The platform needs at least one admin", code="last_admin")
        target.active = False
        await revoke_sessions(db, target.id)
        await audit(db, action="admin.removed", actor=me, tenant_id=None, entity="user", entity_id=target.id, summary=f"{target.name} ({target.phone})")
        await db.commit()
    return _user_row(target, None)
