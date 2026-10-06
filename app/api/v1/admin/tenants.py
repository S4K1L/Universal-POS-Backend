import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_super_admin
from app.core.capabilities import CAPABILITIES
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.security import create_access_token, hash_secret
from app.db.session import get_db
from app.models.catalogue import Product
from app.models.identity import User
from app.models.platform import BusinessType, Plan
from app.models.sales import Sale
from app.models.tenant import Branch, Device, Tenant
from app.schemas.tenant import TenantCreateIn, TenantUpdateIn
from app.services import provisioning, subscription
from app.services.common import audit, utcnow

router = APIRouter(prefix="/admin/tenants", tags=["Platform Console — Businesses"], dependencies=[Depends(require_super_admin)])


async def tenant_rows(db: AsyncSession, tenants: list[Tenant]) -> list[dict]:
    if not tenants:
        return []
    ids = [t.id for t in tenants]
    grace = await subscription.grace_days(db)

    async def grouped(model, *conds):
        rows = (await db.execute(select(model.tenant_id, func.count()).where(model.tenant_id.in_(ids), *conds).group_by(model.tenant_id))).all()
        return {r[0]: int(r[1]) for r in rows}

    staff = await grouped(User, User.active.is_(True))
    branches = await grouped(Branch, Branch.active.is_(True))
    products = await grouped(Product, Product.active.is_(True))
    devices = await grouped(Device, Device.active.is_(True))
    since = utcnow() - timedelta(days=30)
    sales30 = await grouped(Sale, Sale.sold_at >= since, Sale.status == "completed")
    revenue = dict((await db.execute(select(Sale.tenant_id, func.coalesce(func.sum(Sale.total), 0)).where(
        Sale.tenant_id.in_(ids), Sale.sold_at >= since, Sale.status == "completed").group_by(Sale.tenant_id))).all())
    plans = {p.id: p for p in (await db.execute(select(Plan))).scalars()}
    types = {b.id: b for b in (await db.execute(select(BusinessType))).scalars()}
    out = []
    for t in tenants:
        state = subscription.subscription_state(t, grace)
        p, b = plans.get(t.plan_id), types.get(t.business_type_id)
        out.append({
            "id": str(t.id), "name": t.name, "owner_name": t.owner_name, "phone": t.phone, "email": t.email, "city": t.city,
            "business_type": {"key": b.key, "label": b.label, "current_version": b.version} if b else None,
            "template_version": t.template_version, "plan": {"key": p.key, "name": p.name} if p else None,
            "status": t.status, "state": state.state, "billing_cycle": t.billing_cycle, "period_end": t.period_end.isoformat(),
            "days_left": state.days_left, "created_at": t.created_at.isoformat(),
            "last_active_at": t.last_active_at.isoformat() if t.last_active_at else None,
            "staff_count": staff.get(t.id, 0), "branch_count": branches.get(t.id, 0), "product_count": products.get(t.id, 0),
            "device_count": devices.get(t.id, 0), "sales_30d": sales30.get(t.id, 0), "revenue_30d": str(revenue.get(t.id, 0)),
            "capability_overrides": {k: v for k, v in (t.capability_overrides or {}).items() if k in ("enabled", "disabled")},
        })
    return out


@router.get("", summary="Every business on the platform")
async def list_tenants(q: str = "", status: str | None = None, business_type: str | None = None, plan: str | None = None,
                       limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), db: AsyncSession = Depends(get_db)) -> dict:
    conds = []
    if q.strip():
        like = f"%{q.strip()}%"
        conds.append(or_(Tenant.name.ilike(like), Tenant.owner_name.ilike(like), Tenant.phone.ilike(like)))
    if status:
        conds.append(Tenant.status == status)
    if business_type:
        conds.append(Tenant.business_type_id == select(BusinessType.id).where(BusinessType.key == business_type).scalar_subquery())
    if plan:
        conds.append(Tenant.plan_id == select(Plan.id).where(Plan.key == plan).scalar_subquery())
    total = (await db.execute(select(func.count()).select_from(Tenant).where(*conds))).scalar_one()
    rows = (await db.execute(select(Tenant).where(*conds).order_by(Tenant.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": await tenant_rows(db, list(rows)), "total": total, "limit": limit, "offset": offset}


@router.post("", status_code=201, summary="Add a business: pick its type, plan and owner — everything else is provisioned")
async def create_tenant(body: TenantCreateIn, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    tenant, owner, password = await provisioning.provision_tenant(db, admin=admin, payload=body)
    await db.commit()
    row = (await tenant_rows(db, [tenant]))[0]
    return {**row, "owner_login": {"phone": owner.phone, "password": password, "generated": body.password is None}}


@router.get("/{tenant_id}")
async def get_tenant(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    t = await db.get(Tenant, tenant_id)
    if t is None:
        raise NotFoundError("Business not found")
    row = (await tenant_rows(db, [t]))[0]
    plan = await subscription.get_plan(db, t)
    row["capabilities"] = subscription.effective_capabilities(t, plan)
    row["capability_base"] = (t.capability_overrides or {}).get("base", [])
    row["limits"] = plan.limits
    row["usage"] = await subscription.usage(db, t)
    return row


@router.patch("/{tenant_id}", summary="Change plan, status, period or capability overrides")
async def update_tenant(tenant_id: uuid.UUID, body: TenantUpdateIn, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    t = await db.get(Tenant, tenant_id, with_for_update=True)
    if t is None:
        raise NotFoundError("Business not found")
    changes: dict = {}
    if body.name:
        changes["name"] = [t.name, body.name]
        t.name = body.name.strip()
    if body.plan_key:
        plan = (await db.execute(select(Plan).where(Plan.key == body.plan_key))).scalar_one_or_none()
        if plan is None:
            raise NotFoundError("Unknown plan")
        old = await db.get(Plan, t.plan_id)
        changes["plan"] = [old.key if old else None, plan.key]
        t.plan_id = plan.id
    if body.status:
        changes["status"] = [t.status, body.status]
        t.status = body.status
    if body.billing_cycle:
        t.billing_cycle = body.billing_cycle
    if body.period_end:
        changes["period_end"] = [t.period_end.isoformat(), body.period_end.isoformat()]
        t.period_end = body.period_end
    if body.extend_days:
        start = max(t.period_end, utcnow())
        t.period_end = start + timedelta(days=body.extend_days)
        changes["extended_days"] = body.extend_days
    if body.capabilities_enabled is not None or body.capabilities_disabled is not None:
        ov = dict(t.capability_overrides or {})
        for key, vals in (("enabled", body.capabilities_enabled), ("disabled", body.capabilities_disabled)):
            if vals is None:
                continue
            bad = [v for v in vals if v not in CAPABILITIES or CAPABILITIES[v].status != "ready"]
            if bad:
                raise ValidationFailed(f"Capability not available: {bad[0]}", code="validation_failed")
            ov[key] = sorted(set(vals))
            changes[f"capabilities_{key}"] = ov[key]
        t.capability_overrides = ov
    if not changes:
        return (await tenant_rows(db, [t]))[0]
    await audit(db, action="tenant.updated", actor=admin, tenant_id=None, entity="tenant", entity_id=t.id,
                summary=f"{t.name}: " + ", ".join(changes) + (f" — {body.reason}" if body.reason else ""), data=changes)
    await db.commit()
    return (await tenant_rows(db, [t]))[0]


@router.post("/{tenant_id}/apply-template", summary="Bring the business up to its business type's current version")
async def apply_template(tenant_id: uuid.UUID, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    t = await db.get(Tenant, tenant_id, with_for_update=True)
    if t is None:
        raise NotFoundError("Business not found")
    await provisioning.apply_template_update(db, t, admin)
    await db.commit()
    return (await tenant_rows(db, [t]))[0]


@router.post("/{tenant_id}/impersonate", summary="Support session as the owner (audited; 30 minutes)")
async def impersonate(tenant_id: uuid.UUID, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    owner = (await db.execute(select(User).where(User.tenant_id == tenant_id, User.role == "owner", User.active.is_(True)).order_by(User.created_at))).scalars().first()
    if owner is None:
        raise NotFoundError("This business has no active owner")
    await audit(db, action="tenant.impersonated", actor=admin, tenant_id=None, entity="tenant", entity_id=tenant_id, summary=f"Support session as {owner.name}")
    await db.commit()
    token = create_access_token(user_id=owner.id, tenant_id=tenant_id, role=owner.role, impersonated_by=admin.id)
    return {"access_token": token, "token_type": "bearer", "owner": owner.name}


@router.post("/{tenant_id}/reset-owner-password", summary="Generate a new owner password (shown once)")
async def reset_owner_password(tenant_id: uuid.UUID, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    owner = (await db.execute(select(User).where(User.tenant_id == tenant_id, User.role == "owner").order_by(User.created_at))).scalars().first()
    if owner is None:
        raise NotFoundError("This business has no owner")
    password = provisioning.generate_password()
    owner.password_hash = hash_secret(password)
    await audit(db, action="tenant.owner_password_reset", actor=admin, tenant_id=None, entity="user", entity_id=owner.id, summary=owner.name)
    await db.commit()
    return {"phone": owner.phone, "password": password}
