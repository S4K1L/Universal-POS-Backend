"""A business paying for its own subscription. Deliberately not plan-gated: a lapsed account must be able to pay its way back in."""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Ctx, tenant_ctx
from app.api.v1.public import plan_out
from app.core.permissions import Permission
from app.db.session import get_db
from app.models.platform import PaymentChannel, PaymentRequest, Plan, SubscriptionInvoice
from app.schemas.common import Strict
from app.services import billing as svc
from app.services import subscription

router = APIRouter(prefix="/billing", tags=["Billing"])
gate = tenant_ctx(permission=Permission.manage_subscription, allow_lapsed=True, allow_suspended=True)


def channel_out(c: PaymentChannel) -> dict:
    return {"id": str(c.id), "kind": c.kind, "label": c.label, "account_name": c.account_name, "account_number": c.account_number,
            "wallet_type": c.wallet_type, "bank_name": c.bank_name, "branch_name": c.branch_name, "routing_number": c.routing_number,
            "instructions": c.instructions, "qr_payload": c.qr_payload, "active": c.active, "sort_order": c.sort_order}


def request_out(r: PaymentRequest, *, period_end_after: datetime | None = None) -> dict:
    return {
        "id": str(r.id), "reference": f"PAY-{r.seq:06d}", "tenant_id": str(r.tenant_id), "plan_name": r.plan_name,
        "billing_cycle": r.billing_cycle, "amount": str(r.amount), "currency": r.currency, "channel_kind": r.channel_kind,
        "channel_label": r.channel_label, "sender_account": r.sender_account, "transaction_id": r.transaction_id,
        "paid_at": r.paid_at.isoformat() if r.paid_at else None, "note": r.note, "status": r.status,
        "reviewed_by": r.reviewed_by_name, "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None, "review_note": r.review_note,
        "applied_period_end": r.applied_period_end.isoformat() if r.applied_period_end else None,
        "period_end_after": period_end_after.isoformat() if period_end_after else None, "created_at": r.created_at.isoformat(),
    }


class RequestIn(Strict):
    plan_key: str
    billing_cycle: str = Field(pattern="^(monthly|yearly)$")
    channel_id: uuid.UUID
    sender_account: str = Field(default="", max_length=64)
    transaction_id: str = Field(min_length=4, max_length=80)
    paid_at: datetime | None = None
    note: str = Field(default="", max_length=500)


@router.get("/plans", summary="Plans at today's prices, as set by the super admin")
async def plans(ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(Plan).where(Plan.active.is_(True), Plan.public.is_(True)).order_by(Plan.sort_order, Plan.monthly_price))).scalars().all()
    return {"plans": [plan_out(p) for p in rows], "current": ctx.plan.key}


@router.get("/subscription")
async def my_subscription(ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> dict:
    state = subscription.subscription_state(ctx.tenant, await subscription.grace_days(db))
    invoices = (await db.execute(select(SubscriptionInvoice).where(SubscriptionInvoice.tenant_id == ctx.tenant.id).order_by(SubscriptionInvoice.created_at.desc()).limit(24))).scalars().all()
    return {
        "plan": plan_out(ctx.plan), "status": ctx.tenant.status, "state": state.state, "billing_cycle": ctx.tenant.billing_cycle,
        "period_end": ctx.tenant.period_end.isoformat(), "valid_until": state.valid_until.isoformat(), "days_left": state.days_left,
        "usage": await subscription.usage(db, ctx.tenant),
        "invoices": [{"id": str(i.id), "number": f"SUB-{i.seq:06d}", "plan_name": i.plan_name, "billing_cycle": i.billing_cycle, "amount": str(i.amount),
                      "currency": i.currency, "period_start": i.period_start.isoformat(), "period_end": i.period_end.isoformat(),
                      "method": i.method, "reference": i.reference} for i in invoices],
    }


@router.get("/payment-channels", summary="Where to send the money (live channels only)")
async def channels(ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(PaymentChannel).where(PaymentChannel.active.is_(True)).order_by(PaymentChannel.sort_order, PaymentChannel.label))).scalars().all()
    return [channel_out(c) for c in rows]


@router.get("/payment-requests")
async def my_requests(ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(PaymentRequest).where(PaymentRequest.tenant_id == ctx.tenant.id).order_by(PaymentRequest.created_at.desc()).limit(50))).scalars().all()
    return [request_out(r) for r in rows]


@router.post("/payment-requests", status_code=201, summary="File an \"I paid\" claim — the amount is priced on the server")
async def submit(body: RequestIn, ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> dict:
    req = await svc.submit_request(db, tenant=ctx.tenant, user=ctx.user, plan_key=body.plan_key, cycle=body.billing_cycle, channel_id=body.channel_id,
                                   sender_account=body.sender_account, transaction_id=body.transaction_id, paid_at=body.paid_at, note=body.note)
    await db.commit()
    return request_out(req)


@router.post("/payment-requests/{request_id}/cancel")
async def cancel(request_id: uuid.UUID, ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> dict:
    req = await svc.cancel_request(db, tenant=ctx.tenant, user=ctx.user, request_id=request_id)
    await db.commit()
    return request_out(req)
