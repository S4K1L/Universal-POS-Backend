import uuid
from datetime import date
from decimal import Decimal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.money import Money, Qty, q2
from app.models.catalogue import Product, Variant
from app.models.inventory import Purchase, PurchaseLine, PurchasePayment, Supplier
from app.models.tenant import Branch, Tenant
from app.schemas.common import Strict
from app.services import inventory
from app.services.common import audit, next_number, utcnow


class PurchaseLineIn(Strict):
    variant_id: uuid.UUID
    qty: Qty = Field(gt=0)
    unit_cost: Money = Field(ge=0)
    lot_no: str = Field(default="", max_length=64)
    expiry: date | None = None


class PurchaseIn(Strict):
    supplier_id: uuid.UUID
    branch_id: uuid.UUID | None = None
    invoice_no: str = Field(default="", max_length=64)
    purchase_date: date | None = None
    lines: list[PurchaseLineIn] = Field(min_length=1, max_length=300)
    paid: Money = Field(default=Decimal("0"), ge=0)
    pay_method: str = Field(default="cash", max_length=16)
    notes: str = Field(default="", max_length=2000)


class SupplierIn(Strict):
    name: str = Field(min_length=1, max_length=160)
    company: str = Field(default="", max_length=160)
    phone: str = Field(default="", max_length=32)
    address: str = Field(default="", max_length=300)
    notes: str = Field(default="", max_length=2000)
    active: bool = True


class PurchasePaymentIn(Strict):
    amount: Money = Field(gt=0)
    method: str = Field(default="cash", max_length=16)
    note: str = Field(default="", max_length=255)


async def create_purchase(session: AsyncSession, *, tenant: Tenant, user, payload: PurchaseIn) -> Purchase:
    supplier = await session.get(Supplier, payload.supplier_id, with_for_update=True)
    if supplier is None or supplier.tenant_id != tenant.id:
        raise NotFoundError("Supplier not found")
    if payload.branch_id:
        branch = await session.get(Branch, payload.branch_id)
        if branch is None or branch.tenant_id != tenant.id:
            raise NotFoundError("Branch not found")
    else:
        branch = (await session.execute(select(Branch).where(Branch.tenant_id == tenant.id, Branch.is_main.is_(True)))).scalar_one()

    rows = (await session.execute(
        select(Variant, Product).join(Product, Variant.product_id == Product.id)
        .where(Variant.tenant_id == tenant.id, Variant.id.in_([l.variant_id for l in payload.lines]))
    )).all()
    cat = {v.id: (v, p) for v, p in rows}
    total = Decimal("0.00")
    lines: list[PurchaseLine] = []
    for l in payload.lines:
        if l.variant_id not in cat:
            raise NotFoundError(f"Item {l.variant_id} not found")
        variant, product = cat[l.variant_id]
        if product.tracking == "lot" and product.track_expiry and l.expiry is None:
            raise ValidationFailed(f"{product.name} needs an expiry date", code="expiry_required")
        line_total = q2(l.qty * l.unit_cost)
        total += line_total
        lines.append(PurchaseLine(variant_id=variant.id, product_name=product.name + (f" — {variant.label}" if variant.label else ""),
                                  qty=l.qty, unit_cost=l.unit_cost, lot_no=l.lot_no, expiry=l.expiry, line_total=line_total))
    paid = min(payload.paid, total)
    number = await next_number(session, tenant_id=tenant.id, name="purchase", prefix="PO")
    purchase = Purchase(
        tenant_id=tenant.id, branch_id=branch.id, supplier_id=supplier.id, number=number, invoice_no=payload.invoice_no,
        purchase_date=payload.purchase_date or date.today(), status="received", total=q2(total), paid=q2(paid),
        notes=payload.notes, created_by=user.id, lines=lines,
    )
    session.add(purchase)
    await session.flush()
    for l, pl in zip(payload.lines, lines):
        variant, product = cat[l.variant_id]
        await inventory.receive_stock(
            session, tenant_id=tenant.id, branch_id=branch.id, product=product, variant=variant, qty=l.qty,
            unit_cost=l.unit_cost, reason="purchase", lot_no=l.lot_no or number, expiry=l.expiry, ref_type="purchase",
            ref_id=purchase.id, actor_id=user.id,
        )
    if paid > 0:
        session.add(PurchasePayment(tenant_id=tenant.id, purchase_id=purchase.id, amount=q2(paid), method=payload.pay_method, paid_at=utcnow(), paid_by=user.id))
    supplier.balance = q2(supplier.balance + total - paid)
    await audit(session, action="purchase.created", actor=user, tenant_id=tenant.id, entity="purchase", entity_id=purchase.id,
                summary=f"{number} from {supplier.name}: {q2(total)}")
    await session.flush()
    return (await session.execute(
        select(Purchase).options(selectinload(Purchase.lines), selectinload(Purchase.payments)).where(Purchase.id == purchase.id)
        .execution_options(populate_existing=True)
    )).scalar_one()


async def pay_purchase(session: AsyncSession, *, tenant: Tenant, user, purchase_id: uuid.UUID, payload: PurchasePaymentIn) -> Purchase:
    purchase = (await session.execute(
        select(Purchase).options(selectinload(Purchase.lines), selectinload(Purchase.payments))
        .where(Purchase.id == purchase_id, Purchase.tenant_id == tenant.id).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if purchase is None:
        raise NotFoundError("Purchase not found")
    due = q2(purchase.total - purchase.paid)
    if payload.amount > due:
        raise ConflictError(f"{payload.amount} is more than the {due} still owed", code="over_payment")
    supplier = await session.get(Supplier, purchase.supplier_id, with_for_update=True)
    purchase.paid = q2(purchase.paid + payload.amount)
    supplier.balance = q2(supplier.balance - payload.amount)
    session.add(PurchasePayment(tenant_id=tenant.id, purchase_id=purchase.id, amount=payload.amount, method=payload.method,
                                note=payload.note, paid_at=utcnow(), paid_by=user.id))
    await session.flush()
    await session.refresh(purchase, attribute_names=["payments"])
    return purchase
