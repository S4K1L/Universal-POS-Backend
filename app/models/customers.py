import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import SyncTracked, TenantScoped, Timestamps, UUIDPK


class Customer(UUIDPK, TenantScoped, Timestamps, SyncTracked, Base):
    __tablename__ = "customers"
    __sync_entity__ = "customer"
    __table_args__ = (
        Index("ix_customers_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
        Index("ix_customers_phone", "tenant_id", "phone"),
    )

    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(32), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    group: Mapped[str] = mapped_column(String(40), default="")
    credit_limit: Mapped[Decimal] = mapped_column(default=Decimal("0"))  # 0 = no limit set
    balance: Mapped[Decimal] = mapped_column(default=Decimal("0"))  # receivable: what they owe us
    points: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(default=True)


class CustomerLedger(UUIDPK, TenantScoped, Base):
    """Append-only. customers.balance == Σ amount (positive = they owe more)."""

    __tablename__ = "customer_ledger"

    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column()
    kind: Mapped[str] = mapped_column(String(16))  # sale_due|payment|return|adjust|opening
    ref_type: Mapped[str] = mapped_column(String(16), default="")
    ref_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    balance_after: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    at: Mapped[datetime] = mapped_column()
