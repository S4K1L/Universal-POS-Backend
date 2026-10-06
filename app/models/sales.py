import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TenantScoped, Timestamps, UUIDPK


class Sale(UUIDPK, TenantScoped, Base):
    """The id is generated on the till, so a sale keeps one identity from the counter to the server."""

    __tablename__ = "sales"
    __table_args__ = (
        UniqueConstraint("tenant_id", "invoice_no", name="uq_sales_tenant_invoice"),
        Index("ix_sales_branch_time", "tenant_id", "branch_id", "sold_at"),
    )

    server_seq: Mapped[int] = mapped_column(BigInteger, Identity(always=False))
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"), index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"), nullable=True)
    shift_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    invoice_no: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(12), default="completed")  # completed|voided
    sold_at: Mapped[datetime] = mapped_column()
    received_at: Mapped[datetime] = mapped_column()

    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(160), default="Walk-in")
    cashier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    cashier_name: Mapped[str] = mapped_column(String(160), default="")
    salesperson_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    salesperson_name: Mapped[str] = mapped_column(String(160), default="")
    order_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    subtotal: Mapped[Decimal] = mapped_column()  # after line discounts, before bill discount
    discount: Mapped[Decimal] = mapped_column(default=Decimal("0"))  # bill-level
    tax_total: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    rounding: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column()
    paid: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    points_earned: Mapped[int] = mapped_column(Integer, default=0)
    tax_mode: Mapped[str] = mapped_column(String(10), default="inclusive")
    note: Mapped[str] = mapped_column(String(500), default="")
    flags: Mapped[list] = mapped_column(JSONB, default=list)

    lines: Mapped[list["SaleLine"]] = relationship(back_populates="sale", cascade="all, delete-orphan", order_by="SaleLine.position")
    payments: Mapped[list["SalePayment"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan", order_by="SalePayment.paid_at"
    )

    @property
    def due(self) -> Decimal:
        return max(self.total - self.paid, Decimal("0.00"))


class SaleLine(UUIDPK, Base):
    __tablename__ = "sale_lines"

    sale_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("variants.id", ondelete="RESTRICT"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), index=True)
    product_name: Mapped[str] = mapped_column(String(260))
    variant_label: Mapped[str] = mapped_column(String(160), default="")
    sku: Mapped[str] = mapped_column(String(64), default="")
    unit: Mapped[str] = mapped_column(String(24), default="pc")
    unit_factor: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("1"))
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))  # in `unit`
    base_qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))  # qty × unit_factor
    unit_price: Mapped[Decimal] = mapped_column()
    discount: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    tax_amount: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column()  # net of line discount, before bill discount
    charged_total: Mapped[Decimal] = mapped_column(default=Decimal("0"))  # what the customer actually paid for the line (after bill discount, incl. tax if exclusive)
    returned_amount: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))  # per base unit, snapshot
    warranty_months: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(255), default="")
    returned_qty: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))

    sale: Mapped[Sale] = relationship(back_populates="lines")


class SaleLineAllocation(UUIDPK, Base):
    """Which lot a sold unit came from — what makes a recall or an expiry claim traceable."""

    __tablename__ = "sale_line_allocations"

    sale_line_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sale_lines.id", ondelete="CASCADE"), index=True)
    lot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stock_lots.id", ondelete="SET NULL"), nullable=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))


class SalePayment(UUIDPK, TenantScoped, Base):
    """One receipt of money. Invariant: sales.paid == Σ sale_payments.amount."""

    __tablename__ = "sale_payments"
    __table_args__ = (Index("ix_sale_payments_method_time", "tenant_id", "method", "paid_at"),)

    sale_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales.id", ondelete="CASCADE"), index=True)
    method: Mapped[str] = mapped_column(String(16))  # cash|card|bkash|nagad|rocket|upay|bank|…
    amount: Mapped[Decimal] = mapped_column()
    reference: Mapped[str] = mapped_column(String(80), default="")  # bKash trxID, card slip…
    kind: Mapped[str] = mapped_column(String(10), default="sale")  # sale|due|deposit
    received_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    received_by_name: Mapped[str] = mapped_column(String(160), default="")
    device_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    paid_at: Mapped[datetime] = mapped_column()

    sale: Mapped[Sale] = relationship(back_populates="payments")


class SaleReturn(UUIDPK, TenantScoped, Base):
    __tablename__ = "sale_returns"

    sale_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales.id", ondelete="RESTRICT"), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"))
    number: Mapped[str] = mapped_column(String(40), default="")
    refund_total: Mapped[Decimal] = mapped_column()
    credit_applied: Mapped[Decimal] = mapped_column(default=Decimal("0"))  # reduced the customer's due
    cash_refunded: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    refund_method: Mapped[str] = mapped_column(String(16), default="cash")
    restock: Mapped[bool] = mapped_column(default=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    handled_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    handled_by_name: Mapped[str] = mapped_column(String(160), default="")
    returned_at: Mapped[datetime] = mapped_column()

    lines: Mapped[list["SaleReturnLine"]] = relationship(back_populates="sale_return", cascade="all, delete-orphan")


class SaleReturnLine(UUIDPK, Base):
    __tablename__ = "sale_return_lines"

    return_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sale_returns.id", ondelete="CASCADE"), index=True)
    sale_line_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sale_lines.id", ondelete="RESTRICT"))
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("variants.id", ondelete="RESTRICT"))
    product_name: Mapped[str] = mapped_column(String(260))
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    amount: Mapped[Decimal] = mapped_column()

    sale_return: Mapped[SaleReturn] = relationship(back_populates="lines")


class Expense(UUIDPK, TenantScoped, Base):
    __tablename__ = "expenses"

    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    category: Mapped[str] = mapped_column(String(60), default="General")
    amount: Mapped[Decimal] = mapped_column()
    method: Mapped[str] = mapped_column(String(16), default="cash")
    note: Mapped[str] = mapped_column(String(255), default="")
    spent_at: Mapped[datetime] = mapped_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(160), default="")


class Shift(UUIDPK, TenantScoped, Base):
    __tablename__ = "shifts"

    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    opened_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    opened_by_name: Mapped[str] = mapped_column(String(160), default="")
    opened_at: Mapped[datetime] = mapped_column()
    opening_float: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    closed_by_name: Mapped[str] = mapped_column(String(160), default="")
    counted_cash: Mapped[Decimal | None] = mapped_column(nullable=True)
    expected_cash: Mapped[Decimal | None] = mapped_column(nullable=True)
    variance: Mapped[Decimal | None] = mapped_column(nullable=True)
    note: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(8), default="open")
