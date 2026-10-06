import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import Field

from app.core.money import Money, Qty
from app.schemas.common import Out, Strict


class SaleLineIn(Strict):
    id: uuid.UUID
    variant_id: uuid.UUID
    qty: Qty = Field(gt=0)
    unit: str = Field(default="pc", max_length=24)
    unit_factor: Qty = Field(default=Decimal("1"), gt=0)
    unit_price: Money = Field(ge=0)
    discount: Money = Field(default=Decimal("0"), ge=0)
    tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    note: str = Field(default="", max_length=255)


class SalePaymentIn(Strict):
    id: uuid.UUID
    method: str = Field(pattern=r"^[a-z][a-z0-9_]{1,15}$")
    amount: Money = Field(gt=0)
    reference: str = Field(default="", max_length=80)
    paid_at: datetime | None = None


class ClientTotals(Strict):
    subtotal: Money
    discount: Money
    tax_total: Money
    rounding: Money
    total: Money


class SaleCreateIn(Strict):
    id: uuid.UUID
    invoice_no: str = Field(min_length=1, max_length=40)
    sold_at: datetime
    branch_id: uuid.UUID
    shift_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    customer_name: str = Field(default="Walk-in", max_length=160)
    salesperson_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    lines: list[SaleLineIn] = Field(min_length=1, max_length=300)
    bill_discount: Money = Field(default=Decimal("0"), ge=0)
    tax_mode: str = Field(default="inclusive", pattern="^(inclusive|exclusive)$")
    cash_rounding: str = Field(default="none", pattern="^(none|nearest_1)$")
    payments: list[SalePaymentIn] = Field(default_factory=list, max_length=12)
    totals: ClientTotals | None = None
    note: str = Field(default="", max_length=500)


class ReturnLineIn(Strict):
    sale_line_id: uuid.UUID
    qty: Qty = Field(gt=0)


class ReturnIn(Strict):
    id: uuid.UUID
    sale_id: uuid.UUID
    lines: list[ReturnLineIn] = Field(min_length=1)
    restock: bool = True
    refund_method: str = Field(default="cash", pattern=r"^[a-z][a-z0-9_]{1,15}$")
    reason: str = Field(default="", max_length=300)
    returned_at: datetime | None = None


class VoidIn(Strict):
    sale_id: uuid.UUID
    reason: str = Field(default="", max_length=300)


class DuePaymentIn(Strict):
    id: uuid.UUID
    sale_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    amount: Money = Field(gt=0)
    method: str = Field(default="cash", pattern=r"^[a-z][a-z0-9_]{1,15}$")
    reference: str = Field(default="", max_length=80)
    paid_at: datetime | None = None


class CustomerUpsertIn(Strict):
    id: uuid.UUID
    name: str = Field(min_length=1, max_length=160)
    phone: str = Field(default="", max_length=32)
    address: str = Field(default="", max_length=300)
    group: str = Field(default="", max_length=40)
    credit_limit: Money = Field(default=Decimal("0"), ge=0)
    notes: str = Field(default="", max_length=2000)
    attributes: dict = Field(default_factory=dict)


class StockAdjustIn(Strict):
    id: uuid.UUID
    variant_id: uuid.UUID
    branch_id: uuid.UUID
    # Either a signed change, or the counted quantity (then the change is computed on the server).
    delta: Qty | None = None
    counted: Qty | None = None
    reason: str = Field(default="adjust", pattern="^(adjust|damage|expired|count|opening)$")
    note: str = Field(default="", max_length=255)
    lot_no: str = Field(default="", max_length=64)
    expiry: datetime | None = None
    unit_cost: Money | None = None


class ExpenseIn(Strict):
    id: uuid.UUID
    branch_id: uuid.UUID
    category: str = Field(default="General", max_length=60)
    amount: Money = Field(gt=0)
    method: str = Field(default="cash", pattern=r"^[a-z][a-z0-9_]{1,15}$")
    note: str = Field(default="", max_length=255)
    spent_at: datetime | None = None


class ShiftOpenIn(Strict):
    id: uuid.UUID
    branch_id: uuid.UUID
    opened_at: datetime
    opening_float: Money = Field(default=Decimal("0"), ge=0)


class ShiftCloseIn(Strict):
    id: uuid.UUID
    closed_at: datetime
    counted_cash: Money = Field(ge=0)
    note: str = Field(default="", max_length=300)


# ---------------------------------------------------------------- output


class SaleLineOut(Out):
    id: uuid.UUID
    variant_id: uuid.UUID
    product_id: uuid.UUID
    product_name: str
    variant_label: str
    sku: str
    unit: str
    unit_factor: Decimal
    qty: Decimal
    base_qty: Decimal
    unit_price: Decimal
    discount: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    line_total: Decimal
    charged_total: Decimal
    returned_qty: Decimal
    warranty_months: int
    note: str


class SalePaymentOut(Out):
    id: uuid.UUID
    method: str
    amount: Decimal
    reference: str
    kind: str
    received_by_name: str
    paid_at: datetime


class SaleOut(Out):
    id: uuid.UUID
    invoice_no: str
    status: str
    branch_id: uuid.UUID
    device_id: uuid.UUID | None
    sold_at: datetime
    customer_id: uuid.UUID | None
    customer_name: str
    cashier_name: str
    salesperson_name: str
    subtotal: Decimal
    discount: Decimal
    tax_total: Decimal
    rounding: Decimal
    total: Decimal
    paid: Decimal
    due: Decimal
    tax_mode: str
    points_earned: int
    note: str
    flags: list
    lines: list[SaleLineOut]
    payments: list[SalePaymentOut]
