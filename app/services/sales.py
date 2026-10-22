"""Selling: sales, returns, voids and due collection.

A till's completed sale is a *fact* — the customer already walked out with the goods. So the entry
points here validate structure (unknown items, impossible quantities are refused) but anything that
is merely *suspicious* (a price below the catalogue's, a discount the cashier may not give, stock
going negative, a total that doesn't add up) is booked and **flagged** for the owner's exceptions
review instead of being thrown away.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import NotFoundError, RejectEvent
from app.core.money import q2, q3
from app.core.permissions import effective_permissions
from app.models.catalogue import Product, Variant
from app.models.customers import Customer, CustomerLedger
from app.models.identity import User
from app.models.inventory import StockLot
from app.models.sales import Sale, SaleLine, SaleLineAllocation, SalePayment, SaleReturn, SaleReturnLine
from app.models.tenant import Branch, Tenant
from app.schemas.sales import DuePaymentIn, ReturnIn, SaleCreateIn, VoidIn
from app.services import inventory
from app.services.common import audit, next_number, utcnow
from app.services.pricing import LineInput, price_cart
from app.services.subscription import effective_capabilities, get_plan

ZERO = Decimal("0.00")


@dataclass
class Outcome:
    sale: Sale
    duplicate: bool = False
    flags: list[dict] = field(default_factory=list)


def _flag(flags: list[dict], code: str, detail: str = "", **data) -> None:
    flags.append({"code": code, "detail": detail, **data})


async def post_ledger(
    session: AsyncSession, *, tenant_id: uuid.UUID, customer_id: uuid.UUID, amount: Decimal, kind: str,
    ref_type: str = "", ref_id: uuid.UUID | None = None, note: str = "", at: datetime | None = None,
) -> Customer:
    """Append to a customer's ledger and move their balance in the same transaction."""
    result = await session.execute(
        select(Customer).where(Customer.id == customer_id, Customer.tenant_id == tenant_id).with_for_update()
    )
    customer = result.scalar_one_or_none()
    if customer is None:
        raise RejectEvent("Customer not found", code="unknown_customer")
    customer.balance = q2(customer.balance + amount)
    session.add(
        CustomerLedger(
            tenant_id=tenant_id, customer_id=customer.id, amount=q2(amount), kind=kind, ref_type=ref_type,
            ref_id=ref_id, note=note[:255], balance_after=customer.balance, at=at or utcnow(),
        )
    )
    return customer


async def _actor(session: AsyncSession, tenant_id: uuid.UUID, actor_id: uuid.UUID | None, fallback: User) -> User:
    if actor_id is None or actor_id == fallback.id:
        return fallback
    user = await session.get(User, actor_id)
    if user is None or user.tenant_id != tenant_id:
        raise RejectEvent("Unknown cashier on this event", code="unknown_actor")
    return user


def _can(user: User, permission: str) -> bool:
    return permission in {p.value for p in effective_permissions(user.role, user.permissions)}


async def create_sale(
    session: AsyncSession,
    *,
    tenant: Tenant,
    actor: User,
    payload: SaleCreateIn,
    occurred_at: datetime | None = None,
) -> Outcome:
    existing = (
        await session.execute(
            select(Sale).options(selectinload(Sale.lines), selectinload(Sale.payments))
            .where(Sale.id == payload.id, Sale.tenant_id == tenant.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return Outcome(existing, duplicate=True)

    if not _can(actor, "sell"):
        raise RejectEvent("This account may not make sales", code="permission_denied")

    plan = await get_plan(session, tenant)
    caps = set(effective_capabilities(tenant, plan))
    flags: list[dict] = []

    branch = await session.get(Branch, payload.branch_id)
    if branch is None or branch.tenant_id != tenant.id:
        raise RejectEvent("Unknown branch", code="unknown_branch")
    if actor.branch_ids is not None and str(branch.id) not in actor.branch_ids:
        _flag(flags, "branch_not_allowed", f"{actor.name} is not assigned to {branch.name}")

    variant_ids = [l.variant_id for l in payload.lines]
    rows = (
        await session.execute(
            select(Variant, Product).join(Product, Variant.product_id == Product.id)
            .where(Variant.tenant_id == tenant.id, Variant.id.in_(variant_ids))
        )
    ).all()
    catalogue = {v.id: (v, p) for v, p in rows}
    missing = [str(v) for v in variant_ids if v not in catalogue]
    if missing:
        raise RejectEvent("Sale refers to items that are not in this business's catalogue", code="unknown_variant", extra={"variant_ids": missing})

    customer: Customer | None = None
    if payload.customer_id:
        customer = await session.get(Customer, payload.customer_id)
        if customer is None or customer.tenant_id != tenant.id:
            raise RejectEvent("Unknown customer", code="unknown_customer")

    # ---- price it with the shared algorithm, then compare with what the till says it charged
    priced = price_cart(
        [LineInput(l.unit_price, l.qty, l.discount, l.tax_rate) for l in payload.lines],
        bill_discount=payload.bill_discount, tax_mode=payload.tax_mode, cash_rounding=payload.cash_rounding,
    )
    total = priced.total
    if payload.totals is not None:
        t = payload.totals
        if abs(t.total - priced.total) > Decimal("0.01"):
            _flag(flags, "total_mismatch", f"Till charged {t.total}, server computes {priced.total}",
                  client=str(t.total), server=str(priced.total))
            total = t.total  # the receipt is the record of what was charged
        else:
            total = priced.total

    paid = q2(sum((p.amount for p in payload.payments), ZERO))
    if paid > total + Decimal("0.01"):
        raise RejectEvent(f"Payments ({paid}) exceed the bill ({total})", code="over_payment")
    paid = min(paid, total)
    due = q2(total - paid)

    if due > 0:
        if customer is None:
            _flag(flags, "due_without_customer", f"{due} left unpaid with no customer to collect from")
        elif "customer_credit" not in caps:
            _flag(flags, "credit_not_enabled", "Credit sale on a business without customer credit")
        elif customer.credit_limit > 0 and customer.balance + due > customer.credit_limit:
            _flag(flags, "credit_limit_exceeded", f"{customer.name} would owe {customer.balance + due} (limit {customer.credit_limit})")

    now = utcnow()
    sold_at = payload.sold_at
    invoice_no = payload.invoice_no
    clash = (
        await session.execute(select(Sale.id).where(Sale.tenant_id == tenant.id, Sale.invoice_no == invoice_no))
    ).first()
    if clash:
        invoice_no = f"{invoice_no}-{str(payload.id)[:4].upper()}"
        _flag(flags, "invoice_renumbered", f"{payload.invoice_no} was already issued; booked as {invoice_no}")

    can_discount = _can(actor, "apply_discount")
    can_override = _can(actor, "override_price")

    sale = Sale(
        id=payload.id, tenant_id=tenant.id, branch_id=branch.id,
        shift_id=payload.shift_id, invoice_no=invoice_no, status="completed", sold_at=sold_at, received_at=now,
        customer_id=customer.id if customer else None,
        customer_name=(customer.name if customer else payload.customer_name) or "Walk-in",
        cashier_id=actor.id, cashier_name=actor.name, salesperson_id=payload.salesperson_id,
        order_id=payload.order_id, subtotal=priced.subtotal, discount=priced.discount, tax_total=priced.tax_total,
        rounding=priced.rounding, total=total, paid=paid, tax_mode=payload.tax_mode, note=payload.note,
    )
    if payload.salesperson_id:
        sp = await session.get(User, payload.salesperson_id)
        if sp and sp.tenant_id == tenant.id:
            sale.salesperson_name = sp.name
    session.add(sale)
    await session.flush()

    # Lock stock in a stable order so two tills with the same items can't deadlock.
    order = sorted(range(len(payload.lines)), key=lambda i: str(payload.lines[i].variant_id))
    allocations_by_line: dict[int, list[inventory.Allocation]] = {}
    oversold_names: list[str] = []
    line_models: dict[int, SaleLine] = {}

    for idx, (line, pl) in enumerate(zip(payload.lines, priced.lines)):
        variant, product = catalogue[line.variant_id]
        base_qty = q3(line.qty * line.unit_factor)
        if not product.allow_fraction and base_qty != base_qty.to_integral_value():
            _flag(flags, "fractional_qty", f"{product.name} sold in fractions but is not weighed stock")
        if q2(line.unit_price) != q2(variant.price * line.unit_factor) and not can_override:
            _flag(flags, "price_override", f"{product.name}: sold at {line.unit_price}, list {q2(variant.price * line.unit_factor)}",
                  variant_id=str(variant.id))
        if line.discount > 0 and not can_discount:
            _flag(flags, "discount_not_permitted", f"{actor.name} gave a discount of {line.discount} on {product.name}")
        line_models[idx] = SaleLine(
            id=line.id, sale_id=sale.id, position=idx, variant_id=variant.id, product_id=product.id,
            product_name=product.name, variant_label=variant.label, sku=variant.sku, unit=line.unit,
            unit_factor=line.unit_factor, qty=line.qty, base_qty=base_qty, unit_price=line.unit_price,
            discount=line.discount, tax_rate=line.tax_rate, tax_amount=pl.tax, line_total=pl.net,
            charged_total=pl.total, unit_cost=variant.cost, warranty_months=product.warranty_months, note=line.note,
        )

    for idx in order:
        line = payload.lines[idx]
        variant, product = catalogue[line.variant_id]
        base_qty = line_models[idx].base_qty
        allocs, oversold = await inventory.allocate_out(
            session, tenant_id=tenant.id, branch_id=branch.id, product=product, variant=variant, qty=base_qty,
            reason="sale", ref_type="sale", ref_id=sale.id, actor_id=actor.id,
            occurred_at=sold_at,
        )
        allocations_by_line[idx] = allocs
        if allocs:
            total_cost = sum((a.qty * a.unit_cost for a in allocs), Decimal(0))
            line_models[idx].unit_cost = (total_cost / base_qty).quantize(Decimal("0.0001")) if base_qty else variant.cost
        if oversold:
            oversold_names.append(product.name)
    if oversold_names:
        _flag(flags, "oversold", "Stock went below zero: " + ", ".join(oversold_names[:6]))

    for idx, model in line_models.items():
        session.add(model)
    await session.flush()
    for idx, allocs in allocations_by_line.items():
        for a in allocs:
            session.add(SaleLineAllocation(sale_line_id=line_models[idx].id, lot_id=a.lot_id, qty=a.qty))

    pay_total = ZERO
    for p in payload.payments:
        amount = min(p.amount, q2(total - pay_total))
        if amount <= 0:
            continue
        pay_total += amount
        session.add(
            SalePayment(
                id=p.id, tenant_id=tenant.id, sale_id=sale.id, method=p.method, amount=amount, reference=p.reference,
                kind="sale", received_by=actor.id, received_by_name=actor.name,
                paid_at=p.paid_at or sold_at,
            )
        )

    if customer is not None:
        if due > 0:
            await post_ledger(session, tenant_id=tenant.id, customer_id=customer.id, amount=due, kind="sale_due",
                              ref_type="sale", ref_id=sale.id, note=f"Invoice {sale.invoice_no}", at=sold_at)
        loyalty = (tenant.settings or {}).get("loyalty", {})
        if "loyalty" in caps and loyalty.get("enabled") and paid > 0:
            earned = int((paid * Decimal(str(loyalty.get("points_per_100", 0))) / 100).to_integral_value(rounding="ROUND_FLOOR"))
            if earned > 0:
                customer.points += earned
                sale.points_earned = earned

    sale.flags = flags
    await session.flush()
    if flags:
        await audit(session, action="sale.flagged", actor=actor, tenant_id=tenant.id, entity="sale", entity_id=sale.id,
                    summary=f"{sale.invoice_no}: " + ", ".join(f["code"] for f in flags), data={"flags": flags})
    await session.refresh(sale, attribute_names=["lines", "payments"])
    return Outcome(sale, flags=flags)


async def create_return(
    session: AsyncSession, *, tenant: Tenant, actor: User, payload: ReturnIn
) -> SaleReturn:
    existing = await session.get(SaleReturn, payload.id)
    if existing is not None:
        if existing.tenant_id != tenant.id:
            raise RejectEvent("Id already used", code="id_conflict")
        return existing

    if not _can(actor, "refund"):
        raise RejectEvent("This account may not process refunds", code="permission_denied")

    sale = (
        await session.execute(
            select(Sale).options(selectinload(Sale.lines), selectinload(Sale.payments))
            .where(Sale.id == payload.sale_id, Sale.tenant_id == tenant.id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if sale is None:
        raise RejectEvent("Sale not found", code="unknown_sale")
    if sale.status != "completed":
        raise RejectEvent("This sale is voided", code="sale_voided")

    now = payload.returned_at or utcnow()
    by_id = {l.id: l for l in sale.lines}
    ret = SaleReturn(
        id=payload.id, tenant_id=tenant.id, sale_id=sale.id, branch_id=sale.branch_id,
        number=await next_number(session, tenant_id=tenant.id, name="return", prefix="RET"),
        refund_total=ZERO, restock=payload.restock, reason=payload.reason, refund_method=payload.refund_method,
        handled_by=actor.id, handled_by_name=actor.name, returned_at=now,
    )
    session.add(ret)
    await session.flush()

    refund_total = ZERO
    for rl in payload.lines:
        line = by_id.get(rl.sale_line_id)
        if line is None:
            raise RejectEvent("Return line is not part of this sale", code="unknown_line")
        remaining = q3(line.qty - line.returned_qty)
        if rl.qty > remaining:
            raise RejectEvent(f"Only {remaining} of {line.product_name} can still be returned", code="over_return")
        if rl.qty == remaining:
            amount = q2(line.charged_total - line.returned_amount)
        else:
            amount = q2(line.charged_total * rl.qty / line.qty)
        line.returned_qty = q3(line.returned_qty + rl.qty)
        line.returned_amount = q2(line.returned_amount + amount)
        refund_total += amount
        session.add(SaleReturnLine(return_id=ret.id, sale_line_id=line.id, variant_id=line.variant_id,
                                   product_name=line.product_name, qty=rl.qty, amount=amount))

        if payload.restock:
            variant = await session.get(Variant, line.variant_id)
            product = await session.get(Product, line.product_id)
            allocs = (await session.execute(
                select(SaleLineAllocation).where(SaleLineAllocation.sale_line_id == line.id)
            )).scalars().all()
            await inventory.put_back(
                session, tenant_id=tenant.id, branch_id=sale.branch_id, product=product, variant=variant,
                allocations=[(a.lot_id, a.qty) for a in allocs], qty=q3(rl.qty * line.unit_factor), reason="return",
                ref_type="return", ref_id=ret.id, actor_id=actor.id,
                occurred_at=now, unit_cost=line.unit_cost,
            )

    # A refund first cancels what the customer still owes on this invoice; only the rest leaves the till.
    credit = min(sale.due, refund_total)
    if credit > 0:
        session.add(SalePayment(
            tenant_id=tenant.id, sale_id=sale.id, method="return_credit", amount=credit, kind="return",
            received_by=actor.id, received_by_name=actor.name, paid_at=now,
        ))
        sale.paid = q2(sale.paid + credit)
        if sale.customer_id:
            await post_ledger(session, tenant_id=tenant.id, customer_id=sale.customer_id, amount=-credit,
                              kind="return", ref_type="return", ref_id=ret.id, note=f"Return {ret.number}", at=now)
    ret.refund_total = q2(refund_total)
    ret.credit_applied = q2(credit)
    ret.cash_refunded = q2(refund_total - credit)
    await audit(session, action="sale.return", actor=actor, tenant_id=tenant.id, entity="sale", entity_id=sale.id,
                summary=f"{ret.number} on {sale.invoice_no}: refund {ret.refund_total}", at=now)
    await session.flush()
    return ret


async def void_sale(session: AsyncSession, *, tenant: Tenant, actor: User, payload: VoidIn) -> Sale:
    if not _can(actor, "void_sale"):
        raise RejectEvent("This account may not void sales", code="permission_denied")
    sale = (
        await session.execute(
            select(Sale).options(selectinload(Sale.lines), selectinload(Sale.payments))
            .where(Sale.id == payload.sale_id, Sale.tenant_id == tenant.id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if sale is None:
        raise RejectEvent("Sale not found", code="unknown_sale")
    if sale.status == "voided":
        return sale
    now = utcnow()
    for line in sale.lines:
        left = q3(line.qty - line.returned_qty)
        if left <= 0:
            continue
        variant = await session.get(Variant, line.variant_id)
        product = await session.get(Product, line.product_id)
        allocs = (await session.execute(select(SaleLineAllocation).where(SaleLineAllocation.sale_line_id == line.id))).scalars().all()
        await inventory.put_back(
            session, tenant_id=tenant.id, branch_id=sale.branch_id, product=product, variant=variant,
            allocations=[(a.lot_id, a.qty) for a in allocs], qty=q3(left * line.unit_factor), reason="return",
            ref_type="void", ref_id=sale.id, actor_id=actor.id,
            occurred_at=now, unit_cost=line.unit_cost,
        )
    if sale.customer_id and sale.due > 0:
        await post_ledger(session, tenant_id=tenant.id, customer_id=sale.customer_id, amount=-sale.due, kind="return",
                          ref_type="void", ref_id=sale.id, note=f"Void {sale.invoice_no}", at=now)
    sale.status = "voided"
    sale.note = (sale.note + f" [VOID: {payload.reason}]").strip()[:500]
    await audit(session, action="sale.void", actor=actor, tenant_id=tenant.id, entity="sale", entity_id=sale.id,
                summary=f"Voided {sale.invoice_no}: {payload.reason}", at=now)
    await session.flush()
    return sale


async def receive_due_payment(
    session: AsyncSession, *, tenant: Tenant, actor: User, payload: DuePaymentIn
) -> list[Sale]:
    if not _can(actor, "collect_dues"):
        raise RejectEvent("This account may not collect dues", code="permission_denied")
    if not payload.sale_id and not payload.customer_id:
        raise RejectEvent("Give a sale or a customer", code="validation_failed")

    dup = (await session.execute(select(SalePayment.id).where(SalePayment.id == payload.id))).first()
    if dup:
        return []
    paid_at = payload.paid_at or utcnow()
    touched: list[Sale] = []

    async def apply(sale: Sale, amount: Decimal, pay_id: uuid.UUID) -> None:
        session.add(SalePayment(
            id=pay_id, tenant_id=tenant.id, sale_id=sale.id, method=payload.method, amount=amount,
            reference=payload.reference, kind="due", received_by=actor.id, received_by_name=actor.name,
            paid_at=paid_at,
        ))
        sale.paid = q2(sale.paid + amount)
        touched.append(sale)

    if payload.sale_id:
        sale = (await session.execute(
            select(Sale).where(Sale.id == payload.sale_id, Sale.tenant_id == tenant.id).with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if sale is None:
            raise RejectEvent("Sale not found", code="unknown_sale")
        if payload.amount > sale.due:
            raise RejectEvent(f"{payload.amount} is more than the {sale.due} still due on {sale.invoice_no}", code="over_payment")
        await apply(sale, payload.amount, payload.id)
        if sale.customer_id:
            await post_ledger(session, tenant_id=tenant.id, customer_id=sale.customer_id, amount=-payload.amount,
                              kind="payment", ref_type="sale", ref_id=sale.id, note=f"Paid on {sale.invoice_no}", at=paid_at)
        return touched

    # Customer-level: settle the oldest invoices first.
    sales = (await session.execute(
        select(Sale).where(Sale.tenant_id == tenant.id, Sale.customer_id == payload.customer_id, Sale.status == "completed")
        .order_by(Sale.sold_at.asc()).with_for_update().execution_options(populate_existing=True)
    )).scalars().all()
    remaining = payload.amount
    for sale in sales:
        if remaining <= 0:
            break
        if sale.due <= 0:
            continue
        take = min(remaining, sale.due)
        await apply(sale, take, uuid.uuid5(payload.id, str(sale.id)))
        remaining = q2(remaining - take)
    await post_ledger(session, tenant_id=tenant.id, customer_id=payload.customer_id, amount=-payload.amount,
                      kind="payment", ref_type="payment", ref_id=payload.id,
                      note="Payment received" + (f" (advance {remaining})" if remaining > 0 else ""), at=paid_at)
    return touched
