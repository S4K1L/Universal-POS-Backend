import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import SyncTracked, TenantScoped, Timestamps, UUIDPK


class StockBalance(UUIDPK, TenantScoped, Timestamps, SyncTracked, Base):
    """What a branch holds of a variant. Maintained in the same transaction as the ledger row
    that changed it, and property-tested to equal Σ stock_movements.qty_delta."""

    __tablename__ = "stock_balances"
    __sync_entity__ = "stock"
    __table_args__ = (UniqueConstraint("branch_id", "variant_id", name="uq_stock_balances_branch_variant"),)

    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("variants.id", ondelete="CASCADE"), index=True)
    on_hand: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))

    def sync_branch(self) -> uuid.UUID:
        return self.branch_id


class StockLot(UUIDPK, TenantScoped, Timestamps, Base):
    __tablename__ = "stock_lots"
    __table_args__ = (Index("ix_stock_lots_pick", "branch_id", "variant_id", "expiry"),)

    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"))
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("variants.id", ondelete="CASCADE"), index=True)
    lot_no: Mapped[str] = mapped_column(String(64), default="")
    expiry: Mapped[date | None] = mapped_column(nullable=True)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    qty_on_hand: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    received_at: Mapped[datetime] = mapped_column()
    source_ref: Mapped[str] = mapped_column(String(64), default="")


class StockMovement(UUIDPK, TenantScoped, Base):
    """Append-only ledger. Nothing in stock ever changes without one of these."""

    __tablename__ = "stock_movements"
    __table_args__ = (Index("ix_stock_movements_variant_time", "variant_id", "occurred_at"),)

    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("variants.id", ondelete="CASCADE"))
    lot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stock_lots.id", ondelete="SET NULL"), nullable=True)
    qty_delta: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    reason: Mapped[str] = mapped_column(String(24))  # purchase|sale|return|adjust|damage|expired|opening|count
    ref_type: Mapped[str] = mapped_column(String(24), default="")
    ref_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    occurred_at: Mapped[datetime] = mapped_column()
    created_at: Mapped[datetime] = mapped_column()


class Supplier(UUIDPK, TenantScoped, Timestamps, Base):
    __tablename__ = "suppliers"

    name: Mapped[str] = mapped_column(String(160))
    company: Mapped[str] = mapped_column(String(160), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    balance: Mapped[Decimal] = mapped_column(default=Decimal("0"))  # what we owe them
    active: Mapped[bool] = mapped_column(default=True)


class Purchase(UUIDPK, TenantScoped, Timestamps, Base):
    __tablename__ = "purchases"

    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"), index=True)
    number: Mapped[str] = mapped_column(String(32))
    invoice_no: Mapped[str] = mapped_column(String(64), default="")
    purchase_date: Mapped[date] = mapped_column()
    status: Mapped[str] = mapped_column(String(12), default="received")  # draft|received
    total: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    paid: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    lines: Mapped[list["PurchaseLine"]] = relationship(back_populates="purchase", cascade="all, delete-orphan")
    payments: Mapped[list["PurchasePayment"]] = relationship(back_populates="purchase", cascade="all, delete-orphan")


class PurchaseLine(UUIDPK, Base):
    __tablename__ = "purchase_lines"

    purchase_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchases.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("variants.id", ondelete="RESTRICT"))
    product_name: Mapped[str] = mapped_column(String(260))
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    lot_no: Mapped[str] = mapped_column(String(64), default="")
    expiry: Mapped[date | None] = mapped_column(nullable=True)
    line_total: Mapped[Decimal] = mapped_column()

    purchase: Mapped[Purchase] = relationship(back_populates="lines")


class PurchasePayment(UUIDPK, TenantScoped, Base):
    __tablename__ = "purchase_payments"

    purchase_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchases.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column()
    method: Mapped[str] = mapped_column(String(16), default="cash")
    note: Mapped[str] = mapped_column(String(255), default="")
    paid_at: Mapped[datetime] = mapped_column()
    paid_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    purchase: Mapped[Purchase] = relationship(back_populates="payments")
