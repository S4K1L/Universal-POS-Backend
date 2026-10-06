"""Order-based selling (quotation → order with deposit → delivery → invoice). Capability: document_flow."""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TenantScoped, Timestamps, UUIDPK


class Order(UUIDPK, TenantScoped, Timestamps, Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("tenant_id", "number", name="uq_orders_tenant_number"),)

    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"), index=True)
    number: Mapped[str] = mapped_column(String(32))
    # quotation → confirmed (deposit taken) → ready → delivered | cancelled
    status: Mapped[str] = mapped_column(String(12), default="quotation")
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), index=True)
    customer_name: Mapped[str] = mapped_column(String(160), default="")
    salesperson_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    salesperson_name: Mapped[str] = mapped_column(String(160), default="")
    valid_until: Mapped[date | None] = mapped_column(nullable=True)
    delivery_date: Mapped[date | None] = mapped_column(nullable=True)
    delivery_address: Mapped[str] = mapped_column(String(400), default="")
    delivery_charge: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    discount: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    subtotal: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    tax_total: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    deposit_paid: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    terms: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    sale_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)  # the invoice made at delivery
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(160), default="")

    lines: Mapped[list["OrderLine"]] = relationship(back_populates="order", cascade="all, delete-orphan", order_by="OrderLine.position")
    payments: Mapped[list["OrderPayment"]] = relationship(back_populates="order", cascade="all, delete-orphan", order_by="OrderPayment.paid_at")

    @property
    def balance(self) -> Decimal:
        return max(self.total - self.deposit_paid, Decimal("0.00"))


class OrderLine(UUIDPK, Base):
    __tablename__ = "order_lines"

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("variants.id", ondelete="RESTRICT"))
    product_name: Mapped[str] = mapped_column(String(260))
    variant_label: Mapped[str] = mapped_column(String(160), default="")
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit_price: Mapped[Decimal] = mapped_column()
    discount: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column()
    spec_note: Mapped[str] = mapped_column(String(400), default="")  # made-to-order specification
    lead_time_days: Mapped[int] = mapped_column(Integer, default=0)

    order: Mapped[Order] = relationship(back_populates="lines")


class OrderPayment(UUIDPK, TenantScoped, Base):
    __tablename__ = "order_payments"

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    method: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column()
    reference: Mapped[str] = mapped_column(String(80), default="")
    kind: Mapped[str] = mapped_column(String(10), default="deposit")  # deposit|refund
    paid_at: Mapped[datetime] = mapped_column()
    received_by_name: Mapped[str] = mapped_column(String(160), default="")

    order: Mapped[Order] = relationship(back_populates="payments")
