"""Subscription billing: manual, verified by a person.

A shop pays into one of the platform's accounts (bKash, Nagad, bank, Bangla QR…) outside the app,
then files a payment request with the sender's number and the provider's transaction id. A super admin
checks it against the real statement and approves or rejects it. **Approval is the only thing that
moves a subscription.** The amount is priced here from the plan *as it is when the request is filed*
and frozen — editing a plan's price in the console never changes a request already waiting.
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.money import q2
from app.models.identity import User
from app.models.platform import PaymentChannel, PaymentRequest, Plan, SubscriptionInvoice
from app.models.tenant import Tenant
from app.services.common import audit, period_length, utcnow


def price_for(plan: Plan, cycle: str) -> Decimal:
    return q2(plan.yearly_price if cycle == "yearly" else plan.monthly_price)


def next_period_end(tenant: Tenant, plan_id: uuid.UUID, cycle: str, now: datetime | None = None) -> datetime:
    """Renewing the plan you already have, while it still runs, stacks onto its current end — paying early
    never costs days. A plan change, a lapsed account or a trial starts from today (no proration)."""
    now = now or utcnow()
    same_plan_running = tenant.status == "active" and tenant.plan_id == plan_id and tenant.period_end > now
    start = tenant.period_end if same_plan_running else now
    return start + period_length(cycle)


async def submit_request(
    session: AsyncSession, *, tenant: Tenant, user: User, plan_key: str, cycle: str, channel_id: uuid.UUID,
    sender_account: str, transaction_id: str, paid_at: datetime | None, note: str,
) -> PaymentRequest:
    plan = (await session.execute(select(Plan).where(Plan.key == plan_key, Plan.active.is_(True), Plan.public.is_(True)))).scalar_one_or_none()
    if plan is None:
        raise NotFoundError("That plan is not available")
    amount = price_for(plan, cycle)
    if amount <= 0:
        raise ValidationFailed("This plan is free — there is nothing to pay", code="free_plan")
    channel = await session.get(PaymentChannel, channel_id)
    if channel is None or not channel.active:
        raise NotFoundError("That payment channel is not available")

    pending = (await session.execute(
        select(PaymentRequest.id).where(PaymentRequest.tenant_id == tenant.id, PaymentRequest.status == "pending")
    )).first()
    if pending:
        raise ConflictError("You already have a payment waiting for review. Withdraw it first to file another.", code="payment_pending")

    txn = "".join(transaction_id.split()).upper()
    if len(txn) < 4:
        raise ValidationFailed("Enter the transaction ID shown by the provider", code="validation_failed")
    req = PaymentRequest(
        tenant_id=tenant.id, submitted_by=user.id, plan_id=plan.id, plan_name=plan.name, billing_cycle=cycle, amount=amount,
        currency=plan.currency, channel_id=channel.id, channel_kind=channel.kind, channel_label=channel.label,
        sender_account=sender_account.strip(), transaction_id=txn, paid_at=paid_at, note=note.strip(), status="pending",
    )
    session.add(req)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise ConflictError("That transaction ID has already been used", code="duplicate_transaction") from exc
    await audit(session, action="billing.request_submitted", actor=user, tenant_id=tenant.id, entity="payment_request", entity_id=req.id,
                summary=f"{plan.name} {cycle} · {amount} via {channel.label} · {txn}")
    return req


async def cancel_request(session: AsyncSession, *, tenant: Tenant, user: User, request_id: uuid.UUID) -> PaymentRequest:
    req = await session.get(PaymentRequest, request_id, with_for_update=True)
    if req is None or req.tenant_id != tenant.id:
        raise NotFoundError("Payment request not found")
    if req.status != "pending":
        raise ConflictError("Only a request waiting for review can be withdrawn", code="already_reviewed")
    req.status = "cancelled"
    await audit(session, action="billing.request_cancelled", actor=user, tenant_id=tenant.id, entity="payment_request", entity_id=req.id)
    return req


async def approve(session: AsyncSession, *, admin: User, request_id: uuid.UUID, note: str = "") -> PaymentRequest:
    req = await session.get(PaymentRequest, request_id, with_for_update=True)
    if req is None:
        raise NotFoundError("Payment request not found")
    if req.status != "pending":
        raise ConflictError("This request has already been reviewed", code="already_reviewed")
    tenant = await session.get(Tenant, req.tenant_id, with_for_update=True)
    plan = await session.get(Plan, req.plan_id) if req.plan_id else None
    if plan is None:
        raise NotFoundError("The plan on this request no longer exists")
    now = utcnow()
    start = tenant.period_end if (tenant.status == "active" and tenant.plan_id == plan.id and tenant.period_end > now) else now
    new_end = next_period_end(tenant, plan.id, req.billing_cycle, now)
    tenant.plan_id, tenant.billing_cycle, tenant.status, tenant.period_end = plan.id, req.billing_cycle, "active", new_end

    invoice = SubscriptionInvoice(
        tenant_id=tenant.id, tenant_name=tenant.name, plan_name=req.plan_name, billing_cycle=req.billing_cycle,
        amount=req.amount, currency=req.currency, period_start=start, period_end=new_end,
        method=req.channel_label, reference=req.transaction_id, request_id=req.id,
    )
    session.add(invoice)
    await session.flush()
    req.status, req.reviewed_by, req.reviewed_by_name, req.reviewed_at = "approved", admin.id, admin.name, now
    req.review_note, req.applied_period_end, req.invoice_id = note, new_end, invoice.id
    await audit(session, action="billing.approved", actor=admin, tenant_id=None, entity="payment_request", entity_id=req.id,
                summary=f"{tenant.name}: {req.plan_name} {req.billing_cycle} {req.amount} → until {new_end.date()}",
                data={"tenant_id": str(tenant.id), "amount": str(req.amount), "period_end": new_end.isoformat()})
    return req


async def reject(session: AsyncSession, *, admin: User, request_id: uuid.UUID, reason: str) -> PaymentRequest:
    req = await session.get(PaymentRequest, request_id, with_for_update=True)
    if req is None:
        raise NotFoundError("Payment request not found")
    if req.status != "pending":
        raise ConflictError("This request has already been reviewed", code="already_reviewed")
    req.status, req.reviewed_by, req.reviewed_by_name, req.reviewed_at, req.review_note = "rejected", admin.id, admin.name, utcnow(), reason
    await audit(session, action="billing.rejected", actor=admin, tenant_id=None, entity="payment_request", entity_id=req.id,
                summary=f"{req.plan_name} {req.amount}: {reason}")
    return req
