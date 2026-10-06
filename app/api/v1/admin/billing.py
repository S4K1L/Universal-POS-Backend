import uuid
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_super_admin
from app.api.v1.billing import channel_out, request_out
from app.core.errors import NotFoundError, ValidationFailed
from app.core.money import q2
from app.db.session import get_db
from app.models.identity import User
from app.models.platform import AuditLog, PaymentChannel, PaymentRequest, Plan, SubscriptionInvoice
from app.models.sales import Sale
from app.models.tenant import Device, Tenant
from app.schemas.common import Strict
from app.services import billing as svc
from app.services import subscription
from app.services.common import audit, utcnow

router = APIRouter(prefix="/admin", tags=["Platform Console — Billing & Audit"], dependencies=[Depends(require_super_admin)])

# ------------------------------------------------------------------------------------------ channels


class ChannelIn(Strict):
    kind: str = Field(pattern="^(bkash|nagad|rocket|upay|bank|bangla_qr)$")
    label: str = Field(min_length=1, max_length=120)
    account_name: str = Field(default="", max_length=160)
    account_number: str = Field(default="", max_length=64)
    wallet_type: str = Field(default="", pattern="^(|personal|agent|merchant)$")
    bank_name: str = Field(default="", max_length=120)
    branch_name: str = Field(default="", max_length=120)
    routing_number: str = Field(default="", max_length=32)
    instructions: str = Field(default="", max_length=2000)
    qr_payload: str = Field(default="", max_length=4000)
    active: bool = True
    sort_order: int = 0


@router.get("/payment-channels", summary="Accounts the platform collects subscription money into")
async def list_channels(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(PaymentChannel).order_by(PaymentChannel.sort_order, PaymentChannel.label))).scalars().all()
    return [channel_out(c) for c in rows]


@router.post("/payment-channels", status_code=201)
async def create_channel(body: ChannelIn, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    c = PaymentChannel(**body.model_dump())
    db.add(c)
    await db.flush()
    await audit(db, action="channel.created", actor=admin, tenant_id=None, entity="channel", entity_id=c.id, summary=c.label)
    await db.commit()
    return channel_out(c)


@router.put("/payment-channels/{channel_id}")
async def update_channel(channel_id: uuid.UUID, body: ChannelIn, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    c = await db.get(PaymentChannel, channel_id)
    if c is None:
        raise NotFoundError("Channel not found")
    for k, v in body.model_dump().items():
        setattr(c, k, v)
    await audit(db, action="channel.updated", actor=admin, tenant_id=None, entity="channel", entity_id=c.id, summary=c.label)
    await db.commit()
    return channel_out(c)


@router.delete("/payment-channels/{channel_id}", status_code=204)
async def delete_channel(channel_id: uuid.UUID, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> None:
    c = await db.get(PaymentChannel, channel_id)
    if c is None:
        raise NotFoundError("Channel not found")
    await audit(db, action="channel.deleted", actor=admin, tenant_id=None, entity="channel", entity_id=c.id, summary=c.label)
    await db.delete(c)  # past requests keep their snapshot of the label/kind
    await db.commit()


# ------------------------------------------------------------------------------------------ requests


@router.get("/payment-requests", summary="The review queue and history")
async def list_requests(status: str | None = None, limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), db: AsyncSession = Depends(get_db)) -> dict:
    conds = [PaymentRequest.status == status] if status else []
    total = (await db.execute(select(func.count()).select_from(PaymentRequest).where(*conds))).scalar_one()
    rows = (await db.execute(select(PaymentRequest).where(*conds).order_by(
        (PaymentRequest.status == "pending").desc(), PaymentRequest.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    tenants = {t.id: t for t in (await db.execute(select(Tenant).where(Tenant.id.in_([r.tenant_id for r in rows])))).scalars()} if rows else {}
    out = []
    for r in rows:
        t = tenants.get(r.tenant_id)
        after = svc.next_period_end(t, r.plan_id, r.billing_cycle) if (t and r.status == "pending" and r.plan_id) else None
        out.append({**request_out(r, period_end_after=after), "tenant_name": t.name if t else "", "tenant_phone": t.phone if t else ""})
    return {"items": out, "total": total, "limit": limit, "offset": offset}


class RejectIn(Strict):
    reason: str = Field(min_length=3, max_length=500)


class ApproveIn(Strict):
    note: str = Field(default="", max_length=500)


@router.post("/payment-requests/{request_id}/approve", summary="Approve: activates/renews the plan and writes the invoice")
async def approve(request_id: uuid.UUID, body: ApproveIn | None = None, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    req = await svc.approve(db, admin=admin, request_id=request_id, note=body.note if body else "")
    await db.commit()
    return request_out(req)


@router.post("/payment-requests/{request_id}/reject")
async def reject(request_id: uuid.UUID, body: RejectIn, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    req = await svc.reject(db, admin=admin, request_id=request_id, reason=body.reason)
    await db.commit()
    return request_out(req)


@router.get("/invoices", summary="One per approved payment")
async def invoices(limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), db: AsyncSession = Depends(get_db)) -> dict:
    total = (await db.execute(select(func.count()).select_from(SubscriptionInvoice))).scalar_one()
    rows = (await db.execute(select(SubscriptionInvoice).order_by(SubscriptionInvoice.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [{"id": str(i.id), "number": f"SUB-{i.seq:06d}", "tenant_id": str(i.tenant_id), "tenant_name": i.tenant_name, "plan_name": i.plan_name,
                       "billing_cycle": i.billing_cycle, "amount": str(i.amount), "currency": i.currency, "method": i.method, "reference": i.reference,
                       "period_start": i.period_start.isoformat(), "period_end": i.period_end.isoformat(), "created_at": i.created_at.isoformat()} for i in rows],
            "total": total, "limit": limit, "offset": offset}


# --------------------------------------------------------------------------------------------- audit


@router.get("/audit", summary="Append-only trail")
async def audit_log(tenant_id: uuid.UUID | None = None, action: str | None = None, q: str = "", limit: int = Query(default=50, ge=1, le=200),
                    offset: int = Query(default=0, ge=0), db: AsyncSession = Depends(get_db)) -> dict:
    conds = []
    if tenant_id:
        conds.append(AuditLog.tenant_id == tenant_id)
    if action:
        conds.append(AuditLog.action.like(f"{action}%"))
    if q.strip():
        conds.append(AuditLog.summary.ilike(f"%{q.strip()}%"))
    total = (await db.execute(select(func.count()).select_from(AuditLog).where(*conds))).scalar_one()
    rows = (await db.execute(select(AuditLog).where(*conds).order_by(AuditLog.at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [{"id": str(a.id), "at": a.at.isoformat(), "action": a.action, "actor": a.actor_name, "actor_role": a.actor_role, "entity": a.entity,
                       "summary": a.summary, "tenant_id": str(a.tenant_id) if a.tenant_id else None,
                       "impersonated": a.impersonated_by is not None} for a in rows], "total": total, "limit": limit, "offset": offset}


# ------------------------------------------------------------------------------------------- overview


@router.get("/overview", summary="Platform health at a glance")
async def overview(db: AsyncSession = Depends(get_db)) -> dict:
    now = utcnow()
    grace = await subscription.grace_days(db)
    tenants = (await db.execute(select(Tenant))).scalars().all()
    plans = {p.id: p for p in (await db.execute(select(Plan))).scalars()}
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_plan: dict[str, int] = {}
    mrr = Decimal("0")
    expiring_soon = lapsed = 0
    from app.models.platform import BusinessType

    types = {b.id: b.label for b in (await db.execute(select(BusinessType))).scalars()}
    for t in tenants:
        state = subscription.subscription_state(t, grace, now)
        by_status[state.state] = by_status.get(state.state, 0) + 1
        by_type[types.get(t.business_type_id, "?")] = by_type.get(types.get(t.business_type_id, "?"), 0) + 1
        p = plans.get(t.plan_id)
        by_plan[p.name if p else "?"] = by_plan.get(p.name if p else "?", 0) + 1
        if t.status == "active" and not state.lapsed and p:
            mrr += (p.yearly_price / 12) if t.billing_cycle == "yearly" else p.monthly_price
        if not state.lapsed and 0 <= (t.period_end - now).days <= 7:
            expiring_soon += 1
        if state.lapsed:
            lapsed += 1
    pending = (await db.execute(select(func.count()).select_from(PaymentRequest).where(PaymentRequest.status == "pending"))).scalar_one()
    since = now - timedelta(days=180)
    month = func.to_char(SubscriptionInvoice.created_at, "YYYY-MM")
    revenue = (await db.execute(select(month, func.sum(SubscriptionInvoice.amount)).where(SubscriptionInvoice.created_at >= since).group_by(month).order_by(month))).all()
    signups_m = func.to_char(Tenant.created_at, "YYYY-MM")
    signups = (await db.execute(select(signups_m, func.count()).where(Tenant.created_at >= since).group_by(signups_m).order_by(signups_m))).all()
    devices = (await db.execute(select(func.count()).select_from(Device).where(Device.active.is_(True)))).scalar_one()
    stale = (await db.execute(select(func.count()).select_from(Device).where(Device.active.is_(True), (Device.last_sync_at.is_(None)) | (Device.last_sync_at < now - timedelta(days=1))))).scalar_one()
    sales_today = (await db.execute(select(func.count(), func.coalesce(func.sum(Sale.total), 0)).where(Sale.sold_at >= now - timedelta(hours=24), Sale.status == "completed"))).one()
    return {
        "tenants": len(tenants), "by_state": by_status, "by_business_type": by_type, "by_plan": by_plan,
        "mrr": str(q2(mrr)), "pending_payments": int(pending), "expiring_in_7_days": expiring_soon, "lapsed": lapsed,
        "revenue_by_month": [{"month": m, "amount": str(q2(a))} for m, a in revenue],
        "signups_by_month": [{"month": m, "count": int(c)} for m, c in signups],
        "devices": int(devices), "devices_not_synced_24h": int(stale),
        "sales_24h": {"count": int(sales_today[0]), "total": str(q2(sales_today[1]))},
    }
