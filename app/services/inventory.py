"""Stock: an append-only ledger, a balance kept in step with it, and lots for tracked items.

Every change goes through `post_movement`. Balances are locked `FOR UPDATE` while they move, so two
tills selling the last unit cannot both succeed against a blocking policy and the books stay exact.
"""
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import uuid7
from app.core.money import q2, q3
from app.models.catalogue import Product, Variant
from app.models.inventory import Purchase, PurchaseLine, StockBalance, StockLot, StockMovement
from app.services.common import utcnow

ZERO = Decimal("0")


async def balance_for_update(session: AsyncSession, tenant_id: uuid.UUID, branch_id: uuid.UUID, variant_id: uuid.UUID) -> StockBalance:
    # Create-if-missing without a race, then lock the row for the rest of the transaction.
    await session.execute(
        pg_insert(StockBalance)
        .values(id=uuid7(), tenant_id=tenant_id, branch_id=branch_id, variant_id=variant_id, on_hand=ZERO)
        .on_conflict_do_nothing(index_elements=[StockBalance.branch_id, StockBalance.variant_id])
    )
    result = await session.execute(
        select(StockBalance)
        .where(StockBalance.branch_id == branch_id, StockBalance.variant_id == variant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def post_movement(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    branch_id: uuid.UUID,
    variant_id: uuid.UUID,
    qty_delta: Decimal,
    reason: str,
    unit_cost: Decimal = ZERO,
    lot_id: uuid.UUID | None = None,
    ref_type: str = "",
    ref_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    note: str = "",
    occurred_at: datetime | None = None,
) -> StockBalance:
    qty_delta = q3(qty_delta)
    balance = await balance_for_update(session, tenant_id, branch_id, variant_id)
    if qty_delta == 0:
        return balance
    balance.on_hand = q3(balance.on_hand + qty_delta)
    session.add(
        StockMovement(
            tenant_id=tenant_id, branch_id=branch_id, variant_id=variant_id, lot_id=lot_id, qty_delta=qty_delta,
            unit_cost=unit_cost, reason=reason, ref_type=ref_type, ref_id=ref_id, actor_id=actor_id,
            device_id=device_id, note=note[:255], occurred_at=occurred_at or utcnow(), created_at=utcnow(),
        )
    )
    return balance


@dataclass
class Allocation:
    lot_id: uuid.UUID | None
    qty: Decimal
    unit_cost: Decimal


async def allocate_out(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    branch_id: uuid.UUID,
    product: Product,
    variant: Variant,
    qty: Decimal,
    reason: str,
    ref_type: str,
    ref_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    device_id: uuid.UUID | None,
    occurred_at: datetime,
    note: str = "",
) -> tuple[list[Allocation], bool]:
    """Take `qty` base units out of stock. Returns (allocations, oversold).

    Lot-tracked items draw from lots earliest-expiry-first (FEFO; lots without an expiry go last, oldest
    first). Whatever the lots can't cover is still recorded — a sale is a fact — against no lot, and the
    caller is told it was oversold so the sale can be flagged instead of rejected.
    """
    qty = q3(qty)
    if product.type != "stockable":
        return [], False

    allocations: list[Allocation] = []
    remaining = qty

    if product.tracking == "lot":
        result = await session.execute(
            select(StockLot)
            .where(StockLot.branch_id == branch_id, StockLot.variant_id == variant.id, StockLot.qty_on_hand > 0)
            .order_by(StockLot.expiry.asc().nulls_last(), StockLot.received_at.asc())
            .with_for_update()
        )
        for lot in result.scalars():
            if remaining <= 0:
                break
            take = min(remaining, lot.qty_on_hand)
            lot.qty_on_hand = q3(lot.qty_on_hand - take)
            remaining = q3(remaining - take)
            allocations.append(Allocation(lot.id, take, lot.unit_cost))

    if remaining > 0:
        # Not covered by any lot (or the item isn't lot-tracked): book it against no lot.
        allocations.append(Allocation(None, remaining, variant.cost))

    for alloc in allocations:
        await post_movement(
            session, tenant_id=tenant_id, branch_id=branch_id, variant_id=variant.id, qty_delta=-alloc.qty,
            reason=reason, unit_cost=alloc.unit_cost, lot_id=alloc.lot_id, ref_type=ref_type, ref_id=ref_id,
            actor_id=actor_id, device_id=device_id, note=note, occurred_at=occurred_at,
        )
    # "oversold" is only meaningful when the branch balance actually went below zero
    balance = await balance_for_update(session, tenant_id, branch_id, variant.id)
    return allocations, balance.on_hand < 0


async def put_back(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    branch_id: uuid.UUID,
    product: Product,
    variant: Variant,
    allocations: list[tuple[uuid.UUID | None, Decimal]],
    qty: Decimal,
    reason: str,
    ref_type: str,
    ref_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    device_id: uuid.UUID | None,
    occurred_at: datetime,
    unit_cost: Decimal,
) -> None:
    """Return `qty` base units to stock, to the lots they came from first (reverse allocation order)."""
    if product.type != "stockable":
        return
    remaining = q3(qty)
    for lot_id, lot_qty in allocations:
        if remaining <= 0:
            break
        take = min(remaining, lot_qty)
        if lot_id is not None:
            lot = await session.get(StockLot, lot_id, with_for_update=True)
            if lot is not None:
                lot.qty_on_hand = q3(lot.qty_on_hand + take)
        await post_movement(
            session, tenant_id=tenant_id, branch_id=branch_id, variant_id=variant.id, qty_delta=take, reason=reason,
            unit_cost=unit_cost, lot_id=lot_id, ref_type=ref_type, ref_id=ref_id, actor_id=actor_id,
            device_id=device_id, occurred_at=occurred_at,
        )
        remaining = q3(remaining - take)
    if remaining > 0:
        await post_movement(
            session, tenant_id=tenant_id, branch_id=branch_id, variant_id=variant.id, qty_delta=remaining,
            reason=reason, unit_cost=unit_cost, ref_type=ref_type, ref_id=ref_id, actor_id=actor_id,
            device_id=device_id, occurred_at=occurred_at,
        )


async def total_on_hand(session: AsyncSession, variant_id: uuid.UUID) -> Decimal:
    value = (
        await session.execute(
            select(func.coalesce(func.sum(StockBalance.on_hand), 0)).where(StockBalance.variant_id == variant_id, StockBalance.on_hand > 0)
        )
    ).scalar_one()
    return Decimal(value)


async def receive_stock(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    branch_id: uuid.UUID,
    product: Product,
    variant: Variant,
    qty: Decimal,
    unit_cost: Decimal,
    reason: str = "purchase",
    lot_no: str = "",
    expiry: date | None = None,
    ref_type: str = "",
    ref_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    update_average_cost: bool = True,
) -> StockLot | None:
    """Add stock (purchase receipt, opening balance, positive adjustment) and, for the
    weighted-average costing method, fold the new cost into the variant's cost."""
    qty = q3(qty)
    unit_cost = Decimal(unit_cost)
    now = utcnow()
    lot: StockLot | None = None
    if product.tracking == "lot":
        lot = StockLot(
            tenant_id=tenant_id, branch_id=branch_id, variant_id=variant.id, lot_no=lot_no or "", expiry=expiry,
            unit_cost=unit_cost, qty_on_hand=qty, received_at=now, source_ref=str(ref_id or ""),
        )
        session.add(lot)
        await session.flush()

    if update_average_cost and qty > 0:
        existing = await total_on_hand(session, variant.id)
        base = max(existing, ZERO)
        variant.cost = q2((base * variant.cost + qty * unit_cost) / (base + qty)) if (base + qty) > 0 else q2(unit_cost)

    await post_movement(
        session, tenant_id=tenant_id, branch_id=branch_id, variant_id=variant.id, qty_delta=qty, reason=reason,
        unit_cost=unit_cost, lot_id=lot.id if lot else None, ref_type=ref_type, ref_id=ref_id, actor_id=actor_id,
        device_id=device_id, occurred_at=now,
    )
    return lot
