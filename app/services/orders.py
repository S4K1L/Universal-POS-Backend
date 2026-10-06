"""Order-based selling: quotation → confirmed (deposit taken) → ready → delivered (invoice) | cancelled.

Capability: `document_flow`. These operate online from the showroom; the counter till keeps working offline.
Stock is taken and the invoice raised at *delivery*; until then a deposit is held against the order.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.ids import uuid7
from app.core.money import Money, Qty, q2
from app.models.catalogue import Product, Variant
from app.models.customers import Customer
from app.models.identity import User
from app.models.orders import Order, OrderLine, OrderPayment
from app.models.tenant import Branch, Tenant
from app.schemas.common import Strict
from app.schemas.sales import SaleCreateIn, SaleLineIn, SalePaymentIn
from app.services import sales as sales_service
from app.services.common import audit, next_number, utcnow
from app.services.pricing import LineInput, price_cart

ZERO = Decimal("0.00")


class OrderLineIn(Strict):
    variant_id: uuid.UUID
    qty: Qty = Field(gt=0)
    unit_price: Money | None = Field(default=None, ge=0)
    discount: Money = Field(default=ZERO, ge=0)
    spec_note: str = Field(default="", max_length=400)
    lead_time_days: int = Field(default=0, ge=0, le=365)


class OrderIn(Strict):
    customer_id: uuid.UUID
    branch_id: uuid.UUID | None = None
    lines: list[OrderLineIn] = Field(min_length=1, max_length=100)
    discount: Money = Field(default=ZERO, ge=0)
    delivery_charge: Money = Field(default=ZERO, ge=0)
    delivery_date: date | None = None
    delivery_address: str = Field(default="", max_length=400)
    valid_until: date | None = None
    terms: str = Field(default="", max_length=4000)
    note: str = Field(default="", max_length=4000)
    salesperson_id: uuid.UUID | None = None


class DepositIn(Strict):
    amount: Money = Field(gt=0)
    method: str = Field(default="cash", pattern=r"^[a-z][a-z0-9_]{1,15}$")
    reference: str = Field(default="", max_length=80)


class DeliverIn(Strict):
    payments: list[DepositIn] = Field(default_factory=list, max_length=6)
    note: str = Field(default="", max_length=500)


async def _price(session: AsyncSession, tenant: Tenant, lines: list[OrderLineIn], discount: Decimal):
    rows = (await session.execute(
        select(Variant, Product).join(Product, Variant.product_id == Product.id)
        .where(Variant.tenant_id == tenant.id, Variant.id.in_([l.variant_id for l in lines]))
    )).all()
    cat = {v.id: (v, p) for v, p in rows}
    from app.models.catalogue import TaxClass

    tax_rate = {t.id: t.rate for t in (await session.execute(select(TaxClass).where(TaxClass.tenant_id == tenant.id))).scalars()}
    settings = tenant.settings or {}
    inputs, models = [], []
    for i, l in enumerate(lines):
        if l.variant_id not in cat:
            raise NotFoundError(f"Item {l.variant_id} not found")
        v, p = cat[l.variant_id]
        price = l.unit_price if l.unit_price is not None else v.price
        rate = tax_rate.get(p.tax_class_id, Decimal(0)) if p.tax_class_id else Decimal(0)
        inputs.append(LineInput(price, l.qty, l.discount, rate))
        models.append((v, p, price, rate))
    priced = price_cart(inputs, bill_discount=discount, tax_mode=settings.get("tax", {}).get("mode", "inclusive"),
                        cash_rounding="none")  # orders are priced exactly; counter rounding applies to cash sales
    return priced, models


async def _load(session: AsyncSession, tenant: Tenant, order_id: uuid.UUID, *, lock: bool = False) -> Order:
    stmt = (select(Order).options(selectinload(Order.lines), selectinload(Order.payments))
            .where(Order.id == order_id, Order.tenant_id == tenant.id).execution_options(populate_existing=True))
    if lock:
        stmt = stmt.with_for_update()
    order = (await session.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise NotFoundError("Order not found")
    return order


async def create_order(session: AsyncSession, *, tenant: Tenant, user: User, payload: OrderIn) -> Order:
    customer = await session.get(Customer, payload.customer_id)
    if customer is None or customer.tenant_id != tenant.id:
        raise NotFoundError("Customer not found")
    branch = (await session.get(Branch, payload.branch_id)) if payload.branch_id else (
        await session.execute(select(Branch).where(Branch.tenant_id == tenant.id, Branch.is_main.is_(True)))).scalar_one()
    if branch is None or branch.tenant_id != tenant.id:
        raise NotFoundError("Branch not found")
    priced, models = await _price(session, tenant, payload.lines, payload.discount)
    sp_name = ""
    if payload.salesperson_id:
        sp = await session.get(User, payload.salesperson_id)
        if sp and sp.tenant_id == tenant.id:
            sp_name = sp.name
    order = Order(
        tenant_id=tenant.id, branch_id=branch.id, number=await next_number(session, tenant_id=tenant.id, name="order", prefix="ORD"),
        status="quotation", customer_id=customer.id, customer_name=customer.name, salesperson_id=payload.salesperson_id,
        salesperson_name=sp_name or user.name, valid_until=payload.valid_until, delivery_date=payload.delivery_date,
        delivery_address=payload.delivery_address or customer.address, delivery_charge=payload.delivery_charge,
        discount=priced.discount, subtotal=priced.subtotal, tax_total=priced.tax_total,
        total=q2(priced.total + payload.delivery_charge), terms=payload.terms, note=payload.note,
        created_by=user.id, created_by_name=user.name,
    )
    for i, (l, (v, p, price, rate), pl) in enumerate(zip(payload.lines, models, priced.lines)):
        order.lines.append(OrderLine(
            position=i, variant_id=v.id, product_name=p.name, variant_label=v.label, qty=l.qty, unit_price=price,
            discount=l.discount, tax_rate=rate, line_total=pl.total, spec_note=l.spec_note, lead_time_days=l.lead_time_days,
        ))
    session.add(order)
    await session.flush()
    await audit(session, action="order.created", actor=user, tenant_id=tenant.id, entity="order", entity_id=order.id, summary=f"{order.number} for {customer.name}: {order.total}")
    return await _load(session, tenant, order.id)


async def replace_order(session: AsyncSession, *, tenant: Tenant, user: User, order_id: uuid.UUID, payload: OrderIn) -> Order:
    order = await _load(session, tenant, order_id, lock=True)
    if order.status != "quotation":
        raise ConflictError("Only a quotation can be edited. Cancel and re-quote a confirmed order.", code="order_locked")
    priced, models = await _price(session, tenant, payload.lines, payload.discount)
    order.lines.clear()
    await session.flush()
    for i, (l, (v, p, price, rate), pl) in enumerate(zip(payload.lines, models, priced.lines)):
        order.lines.append(OrderLine(position=i, variant_id=v.id, product_name=p.name, variant_label=v.label, qty=l.qty,
                                     unit_price=price, discount=l.discount, tax_rate=rate, line_total=pl.total,
                                     spec_note=l.spec_note, lead_time_days=l.lead_time_days))
    order.discount, order.subtotal, order.tax_total = priced.discount, priced.subtotal, priced.tax_total
    order.delivery_charge, order.total = payload.delivery_charge, q2(priced.total + payload.delivery_charge)
    order.delivery_date, order.valid_until, order.terms, order.note = payload.delivery_date, payload.valid_until, payload.terms, payload.note
    if payload.delivery_address:
        order.delivery_address = payload.delivery_address
    await session.flush()
    return await _load(session, tenant, order.id)


async def add_deposit(session: AsyncSession, *, tenant: Tenant, user: User, order_id: uuid.UUID, payload: DepositIn, confirm: bool = False) -> Order:
    order = await _load(session, tenant, order_id, lock=True)
    if order.status in ("delivered", "cancelled"):
        raise ConflictError("This order is closed", code="order_closed")
    if payload.amount > order.balance:
        raise ConflictError(f"{payload.amount} is more than the {order.balance} balance on this order", code="over_payment")
    order.payments.append(OrderPayment(tenant_id=tenant.id, method=payload.method, amount=payload.amount, reference=payload.reference,
                                       kind="deposit", paid_at=utcnow(), received_by_name=user.name))
    order.deposit_paid = q2(order.deposit_paid + payload.amount)
    if order.status == "quotation":
        minimum = q2(order.total * Decimal((tenant.settings or {}).get("deposit_percent_min", 0)) / 100)
        if order.deposit_paid < minimum:
            raise ValidationFailed(f"A deposit of at least {minimum} ({(tenant.settings or {}).get('deposit_percent_min', 0)}%) is needed to confirm this order",
                                   code="deposit_too_low", extra={"minimum": str(minimum)})
        order.status = "confirmed"
        await audit(session, action="order.confirmed", actor=user, tenant_id=tenant.id, entity="order", entity_id=order.id,
                    summary=f"{order.number} confirmed with deposit {order.deposit_paid}")
    await session.flush()
    return await _load(session, tenant, order.id)


async def set_status(session: AsyncSession, *, tenant: Tenant, user: User, order_id: uuid.UUID, status: str) -> Order:
    order = await _load(session, tenant, order_id, lock=True)
    allowed = {"confirmed": ["ready"], "ready": ["confirmed"]}
    if status not in allowed.get(order.status, []):
        raise ConflictError(f"A {order.status} order cannot move to {status}", code="invalid_transition")
    order.status = status
    await session.flush()
    return await _load(session, tenant, order.id)


async def cancel_order(session: AsyncSession, *, tenant: Tenant, user: User, order_id: uuid.UUID, reason: str) -> Order:
    order = await _load(session, tenant, order_id, lock=True)
    if order.status in ("delivered", "cancelled"):
        raise ConflictError("This order is already closed", code="order_closed")
    if order.deposit_paid > 0:
        order.payments.append(OrderPayment(tenant_id=tenant.id, method="cash", amount=order.deposit_paid, kind="refund",
                                           reference="cancellation", paid_at=utcnow(), received_by_name=user.name))
    order.status = "cancelled"
    order.note = (order.note + f"\n[Cancelled: {reason}]").strip()
    await audit(session, action="order.cancelled", actor=user, tenant_id=tenant.id, entity="order", entity_id=order.id,
                summary=f"{order.number} cancelled; deposit {order.deposit_paid} to refund: {reason}")
    await session.flush()
    return await _load(session, tenant, order.id)


async def _delivery_variant(session: AsyncSession, tenant: Tenant) -> Variant:
    existing = (await session.execute(select(Variant).where(Variant.tenant_id == tenant.id, Variant.sku == "DELIVERY"))).scalar_one_or_none()
    if existing:
        return existing
    product = Product(tenant_id=tenant.id, name="Delivery charge", type="service", base_unit="pc", attributes={})
    session.add(product)
    await session.flush()
    variant = Variant(tenant_id=tenant.id, product_id=product.id, sku="DELIVERY", label="", price=ZERO)
    session.add(variant)
    await session.flush()
    return variant


async def deliver(session: AsyncSession, *, tenant: Tenant, user: User, order_id: uuid.UUID, payload: DeliverIn):
    """Raise the invoice, take the stock, and apply deposits + the final payment."""
    order = await _load(session, tenant, order_id, lock=True)
    if order.status not in ("confirmed", "ready"):
        raise ConflictError("Only a confirmed order can be delivered", code="invalid_transition")
    now = utcnow()
    final = q2(sum((p.amount for p in payload.payments), ZERO))
    if final > order.balance:
        raise ConflictError(f"{final} is more than the {order.balance} balance", code="over_payment")

    lines = [SaleLineIn(id=uuid7(), variant_id=l.variant_id, qty=l.qty, unit="pc", unit_price=l.unit_price, discount=l.discount,
                        tax_rate=l.tax_rate) for l in order.lines]
    if order.delivery_charge > 0:
        dv = await _delivery_variant(session, tenant)
        lines.append(SaleLineIn(id=uuid7(), variant_id=dv.id, qty=Decimal("1"), unit="pc", unit_price=order.delivery_charge))
    payments = [SalePaymentIn(id=uuid7(), method=p.method, amount=p.amount, reference=p.reference, paid_at=p.paid_at)
                for p in order.payments if p.kind == "deposit"]
    payments += [SalePaymentIn(id=uuid7(), method=p.method, amount=p.amount, reference=p.reference, paid_at=now) for p in payload.payments]
    settings = tenant.settings or {}
    sale_in = SaleCreateIn(
        id=uuid7(), invoice_no=await next_number(session, tenant_id=tenant.id, name="invoice", prefix="INV"), sold_at=now,
        branch_id=order.branch_id, customer_id=order.customer_id, customer_name=order.customer_name,
        salesperson_id=order.salesperson_id, order_id=order.id, lines=lines, bill_discount=order.discount,
        tax_mode=settings.get("tax", {}).get("mode", "inclusive"), cash_rounding="none", payments=payments, note=payload.note or f"Order {order.number}",
    )
    outcome = await sales_service.create_sale(session, tenant=tenant, actor=user, device=None, payload=sale_in)
    order.status, order.sale_id = "delivered", outcome.sale.id
    order.deposit_paid = q2(order.deposit_paid + final)
    for p in payload.payments:
        order.payments.append(OrderPayment(tenant_id=tenant.id, method=p.method, amount=p.amount, reference=p.reference, kind="deposit",
                                           paid_at=now, received_by_name=user.name))
    await audit(session, action="order.delivered", actor=user, tenant_id=tenant.id, entity="order", entity_id=order.id,
                summary=f"{order.number} delivered → {outcome.sale.invoice_no}")
    await session.flush()
    return await _load(session, tenant, order.id), outcome.sale
