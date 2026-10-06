"""Offline sync: devices push events, pull changes.

PUSH  Every event carries a client-generated UUID. It is applied inside its own SAVEPOINT, so one bad
      event never poisons the batch, and recorded in `sync_events` under a unique (tenant, event id) —
      replaying a push (a flaky network does this constantly) is harmless and returns the original result.
      Structurally unusable events come back `rejected` and stay in the device's outbox for a human.

PULL  The server keeps an ordered change log (`sync_changes`, written by a before_flush hook). A device
      sends the last `seq` it saw and receives the *current* state of everything changed since, plus
      deletions. Catalogue wins on the server; devices never edit it offline.
"""
import uuid
from datetime import date

from pydantic import ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import RejectEvent
from app.core.money import q2, q3
from app.core.permissions import effective_permissions
from app.models.catalogue import Category, Product, TaxClass, Variant
from app.models.customers import Customer
from app.models.identity import User
from app.models.inventory import StockBalance
from app.models.sales import Expense, Shift
from app.models.sync import SyncChange, SyncEvent
from app.models.tenant import Branch, Device, Tenant
from app.schemas.sales import (
    CustomerUpsertIn, DuePaymentIn, ExpenseIn, ReturnIn, SaleCreateIn, ShiftCloseIn, ShiftOpenIn, StockAdjustIn, VoidIn,
)
from app.schemas.sync import PushIn, SyncEventIn
from app.services import inventory, sales as sales_service, serializers
from app.services.common import audit, utcnow

PULL_PAGE = 500


# ------------------------------------------------------------------------------------------ push


def _validate(model, payload: dict):
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise RejectEvent(f"Invalid {first['loc'][-1] if first['loc'] else 'payload'}: {first['msg']}", code="validation_failed") from exc


async def _can(user: User, permission: str) -> bool:
    return permission in {p.value for p in effective_permissions(user.role, user.permissions)}


async def _h_sale_create(session, tenant, actor, device, ev: SyncEventIn) -> dict:
    payload = _validate(SaleCreateIn, ev.payload)
    outcome = await sales_service.create_sale(session, tenant=tenant, actor=actor, device=device, payload=payload)
    return {
        "invoice_no": outcome.sale.invoice_no, "server_seq": outcome.sale.server_seq,
        "flags": [f["code"] for f in outcome.flags], "duplicate": outcome.duplicate,
    }


async def _h_sale_return(session, tenant, actor, device, ev):
    ret = await sales_service.create_return(session, tenant=tenant, actor=actor, device=device, payload=_validate(ReturnIn, ev.payload))
    return {"number": ret.number, "refund_total": str(ret.refund_total)}


async def _h_sale_void(session, tenant, actor, device, ev):
    sale = await sales_service.void_sale(session, tenant=tenant, actor=actor, device=device, payload=_validate(VoidIn, ev.payload))
    return {"invoice_no": sale.invoice_no}


async def _h_payment_receive(session, tenant, actor, device, ev):
    touched = await sales_service.receive_due_payment(session, tenant=tenant, actor=actor, device=device, payload=_validate(DuePaymentIn, ev.payload))
    return {"invoices": [s.invoice_no for s in touched]}


async def _h_customer_upsert(session, tenant, actor, device, ev):
    p = _validate(CustomerUpsertIn, ev.payload)
    if not await _can(actor, "manage_customers"):
        raise RejectEvent("This account may not edit customers", code="permission_denied")
    customer = await session.get(Customer, p.id)
    if customer is not None and customer.tenant_id != tenant.id:
        raise RejectEvent("Id already used", code="id_conflict")
    if customer is None:
        customer = Customer(id=p.id, tenant_id=tenant.id, name=p.name)
        session.add(customer)
    customer.name, customer.phone, customer.address = p.name.strip(), p.phone.strip(), p.address.strip()
    customer.group, customer.notes, customer.attributes = p.group, p.notes, p.attributes
    customer.credit_limit = p.credit_limit
    return {"id": str(customer.id)}


async def _h_stock_adjust(session, tenant, actor, device, ev):
    p = _validate(StockAdjustIn, ev.payload)
    if not await _can(actor, "adjust_stock"):
        raise RejectEvent("This account may not adjust stock", code="permission_denied")
    branch = await session.get(Branch, p.branch_id)
    if branch is None or branch.tenant_id != tenant.id:
        raise RejectEvent("Unknown branch", code="unknown_branch")
    row = (await session.execute(
        select(Variant, Product).join(Product, Variant.product_id == Product.id)
        .where(Variant.id == p.variant_id, Variant.tenant_id == tenant.id)
    )).first()
    if row is None:
        raise RejectEvent("Unknown item", code="unknown_variant")
    variant, product = row
    if p.delta is None and p.counted is None:
        raise RejectEvent("Give a change or a counted quantity", code="validation_failed")
    if p.counted is not None:
        bal = await inventory.balance_for_update(session, tenant.id, branch.id, variant.id)
        delta = q3(p.counted - bal.on_hand)
        reason = "count"
    else:
        delta, reason = q3(p.delta), p.reason
    if delta == 0:
        return {"delta": "0.000"}
    if delta > 0:
        await inventory.receive_stock(
            session, tenant_id=tenant.id, branch_id=branch.id, product=product, variant=variant, qty=delta,
            unit_cost=p.unit_cost if p.unit_cost is not None else variant.cost, reason=reason, lot_no=p.lot_no or "ADJ",
            expiry=p.expiry.date() if p.expiry else None, ref_type="adjust", ref_id=p.id, actor_id=actor.id,
            device_id=device.id if device else None, update_average_cost=p.unit_cost is not None,
        )
    else:
        await inventory.allocate_out(
            session, tenant_id=tenant.id, branch_id=branch.id, product=product, variant=variant, qty=-delta,
            reason=reason, ref_type="adjust", ref_id=p.id, actor_id=actor.id, device_id=device.id if device else None,
            occurred_at=ev.occurred_at, note=p.note,
        )
    await audit(session, action="stock.adjust", actor=actor, tenant_id=tenant.id, entity="variant", entity_id=variant.id,
                summary=f"{product.name}: {delta:+} ({reason}) {p.note}".strip(), at=ev.occurred_at)
    return {"delta": str(delta)}


async def _h_expense_create(session, tenant, actor, device, ev):
    p = _validate(ExpenseIn, ev.payload)
    if not await _can(actor, "manage_expenses"):
        raise RejectEvent("This account may not record expenses", code="permission_denied")
    branch = await session.get(Branch, p.branch_id)
    if branch is None or branch.tenant_id != tenant.id:
        raise RejectEvent("Unknown branch", code="unknown_branch")
    if await session.get(Expense, p.id) is None:
        session.add(Expense(
            id=p.id, tenant_id=tenant.id, branch_id=branch.id, device_id=device.id if device else None,
            category=p.category, amount=p.amount, method=p.method, note=p.note, spent_at=p.spent_at or ev.occurred_at,
            created_by=actor.id, created_by_name=actor.name,
        ))
    return {"id": str(p.id)}


async def _h_shift_open(session, tenant, actor, device, ev):
    p = _validate(ShiftOpenIn, ev.payload)
    if not await _can(actor, "manage_shifts"):
        raise RejectEvent("This account may not open a shift", code="permission_denied")
    if await session.get(Shift, p.id) is None:
        session.add(Shift(
            id=p.id, tenant_id=tenant.id, branch_id=p.branch_id, device_id=device.id if device else None,
            opened_by=actor.id, opened_by_name=actor.name, opened_at=p.opened_at, opening_float=p.opening_float, status="open",
        ))
    return {"id": str(p.id)}


async def _h_shift_close(session, tenant, actor, device, ev):
    from app.services.reports import shift_expected_cash

    p = _validate(ShiftCloseIn, ev.payload)
    shift = await session.get(Shift, p.id)
    if shift is None or shift.tenant_id != tenant.id:
        raise RejectEvent("Shift not found (its open event may not have arrived yet)", code="unknown_shift")
    if shift.status == "closed":
        return {"variance": str(shift.variance)}
    expected = await shift_expected_cash(session, shift, until=p.closed_at)
    shift.closed_at, shift.closed_by_name, shift.counted_cash = p.closed_at, actor.name, p.counted_cash
    shift.expected_cash, shift.variance = expected, q2(p.counted_cash - expected)
    shift.note, shift.status = p.note, "closed"
    return {"expected": str(expected), "variance": str(shift.variance)}


HANDLERS = {
    "sale.create": _h_sale_create,
    "sale.return": _h_sale_return,
    "sale.void": _h_sale_void,
    "payment.receive": _h_payment_receive,
    "customer.upsert": _h_customer_upsert,
    "stock.adjust": _h_stock_adjust,
    "expense.create": _h_expense_create,
    "shift.open": _h_shift_open,
    "shift.close": _h_shift_close,
}


async def push(session: AsyncSession, *, tenant: Tenant, user: User, payload: PushIn) -> list[dict]:
    device = await session.get(Device, payload.device_id)
    if device is None or device.tenant_id != tenant.id or not device.active:
        raise RejectEvent("This device is not registered (or was removed). Register it again.", code="unknown_device")

    results: list[dict] = []
    for ev in payload.events:
        existing = (await session.execute(
            select(SyncEvent).where(SyncEvent.tenant_id == tenant.id, SyncEvent.event_id == ev.id)
        )).scalar_one_or_none()
        if existing is not None:
            results.append({"id": str(ev.id), "status": "duplicate", **existing.result})
            continue

        handler = HANDLERS.get(ev.type)
        if handler is None:
            results.append({"id": str(ev.id), "status": "rejected", "code": "unknown_event_type", "message": f"Unknown event type {ev.type}"})
            continue
        try:
            async with session.begin_nested():
                from app.services.sales import _actor  # local import: tiny helper shared with sales

                actor = await _actor(session, tenant.id, ev.actor_id, user)
                result = await handler(session, tenant, actor, device, ev)
                session.add(SyncEvent(tenant_id=tenant.id, event_id=ev.id, device_id=device.id, type=ev.type, result=result))
                await session.flush()
            results.append({"id": str(ev.id), "status": "applied", **result})
        except RejectEvent as exc:
            results.append({"id": str(ev.id), "status": "rejected", "code": exc.code, "message": exc.detail, **exc.extra})
        except IntegrityError:
            # A concurrent retry of the same event won the race — report what it stored.
            session.expire_all()
            stored = (await session.execute(
                select(SyncEvent).where(SyncEvent.tenant_id == tenant.id, SyncEvent.event_id == ev.id)
            )).scalar_one_or_none()
            if stored is not None:
                results.append({"id": str(ev.id), "status": "duplicate", **stored.result})
            else:
                results.append({"id": str(ev.id), "status": "rejected", "code": "conflict", "message": "Could not apply this event; it conflicts with existing data."})

    now = utcnow()
    device.last_sync_at = now
    device.last_seen_at = now
    return results


# ------------------------------------------------------------------------------------------ pull


async def compact_changes(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Drop change rows superseded by a newer one for the same entity. A device with an old cursor
    still receives the latest row for everything that changed, so this never loses information."""
    result = await session.execute(
        text(
            "DELETE FROM sync_changes a USING sync_changes b "
            "WHERE a.tenant_id = :t AND b.tenant_id = :t AND a.entity = b.entity "
            "AND a.entity_id = b.entity_id AND a.seq < b.seq"
        ),
        {"t": tenant_id},
    )
    return result.rowcount or 0


async def pull(
    session: AsyncSession, *, tenant: Tenant, user: User, device: Device, cursor: int, limit: int = PULL_PAGE
) -> dict:
    if cursor <= 0:
        await compact_changes(session, tenant.id)
    limit = max(1, min(limit, 1000))
    rows = (await session.execute(
        select(SyncChange)
        .where(
            SyncChange.tenant_id == tenant.id, SyncChange.seq > cursor,
            (SyncChange.branch_id.is_(None)) | (SyncChange.branch_id == device.branch_id),
        )
        .order_by(SyncChange.seq).limit(limit + 1)
    )).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1].seq if rows else cursor

    # last change wins per (entity, id)
    latest: dict[tuple[str, uuid.UUID], SyncChange] = {}
    for r in rows:
        latest[(r.entity, r.entity_id)] = r

    ids: dict[str, list[uuid.UUID]] = {}
    for (entity, eid), r in latest.items():
        if r.op != "delete":
            ids.setdefault(entity, []).append(eid)

    include_cost = await _can(user, "view_cost")
    loaded: dict[tuple[str, uuid.UUID], dict] = {}
    if ids.get("product"):
        for p in (await session.execute(
            select(Product).options(selectinload(Product.variants).selectinload(Variant.barcodes))
            .where(Product.tenant_id == tenant.id, Product.id.in_(ids["product"]))
        )).scalars():
            loaded[("product", p.id)] = serializers.product_out(p, include_cost=include_cost)
    if ids.get("category"):
        for c in (await session.execute(select(Category).where(Category.tenant_id == tenant.id, Category.id.in_(ids["category"])))).scalars():
            loaded[("category", c.id)] = serializers.category_out(c)
    if ids.get("tax_class"):
        for t in (await session.execute(select(TaxClass).where(TaxClass.tenant_id == tenant.id, TaxClass.id.in_(ids["tax_class"])))).scalars():
            loaded[("tax_class", t.id)] = serializers.tax_class_out(t)
    if ids.get("customer"):
        for c in (await session.execute(select(Customer).where(Customer.tenant_id == tenant.id, Customer.id.in_(ids["customer"])))).scalars():
            loaded[("customer", c.id)] = serializers.customer_out(c)
    if ids.get("stock"):
        for b in (await session.execute(select(StockBalance).where(StockBalance.tenant_id == tenant.id, StockBalance.id.in_(ids["stock"])))).scalars():
            loaded[("stock", b.id)] = serializers.stock_out(b)

    changes = []
    for (entity, eid), r in sorted(latest.items(), key=lambda kv: kv[1].seq):
        if entity == "context":
            changes.append({"seq": r.seq, "entity": "context", "id": str(eid), "op": "upsert"})
        elif r.op == "delete" or (entity, eid) not in loaded:
            changes.append({"seq": r.seq, "entity": entity, "id": str(eid), "op": "delete"})
        else:
            changes.append({"seq": r.seq, "entity": entity, "id": str(eid), "op": "upsert", "data": loaded[(entity, eid)]})

    device.last_seen_at = utcnow()
    return {"cursor": next_cursor, "has_more": has_more, "changes": changes, "server_time": utcnow().isoformat()}
