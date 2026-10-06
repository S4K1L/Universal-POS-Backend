import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import Timestamps, UUIDPK


class BusinessType(UUIDPK, Timestamps, Base):
    """A versioned template: capabilities + attribute definitions + stock/tax/POS defaults."""

    __tablename__ = "business_types"

    key: Mapped[str] = mapped_column(String(40), unique=True)
    label: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    family: Mapped[str] = mapped_column(String(32), default="counter_retail")
    version: Mapped[int] = mapped_column(Integer, default=1)
    definition: Mapped[dict] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Plan(UUIDPK, Timestamps, Base):
    """A subscription plan. Prices, limits and features are edited by the super admin — never in code."""

    __tablename__ = "plans"

    key: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    tagline: Mapped[str] = mapped_column(String(200), default="")
    monthly_price: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    yearly_price: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3), default="BDT")
    trial_days: Mapped[int] = mapped_column(Integer, default=0)
    limits: Mapped[dict] = mapped_column(JSONB, default=dict)  # -1 = unlimited
    features: Mapped[list] = mapped_column(JSONB, default=list)
    capabilities: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # None = all capabilities
    highlight: Mapped[bool] = mapped_column(default=False)
    public: Mapped[bool] = mapped_column(default=True)
    active: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    updated_by: Mapped[str] = mapped_column(String(120), default="")


class PaymentChannel(UUIDPK, Timestamps, Base):
    """An account the platform collects subscription money into (bKash, Nagad, bank, Bangla QR…)."""

    __tablename__ = "payment_channels"

    kind: Mapped[str] = mapped_column(String(24))  # bkash|nagad|rocket|upay|bank|bangla_qr
    label: Mapped[str] = mapped_column(String(120))
    account_name: Mapped[str] = mapped_column(String(160), default="")
    account_number: Mapped[str] = mapped_column(String(64), default="")
    wallet_type: Mapped[str] = mapped_column(String(24), default="")  # personal|agent|merchant
    bank_name: Mapped[str] = mapped_column(String(120), default="")
    branch_name: Mapped[str] = mapped_column(String(120), default="")
    routing_number: Mapped[str] = mapped_column(String(32), default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    qr_payload: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class PaymentRequest(UUIDPK, Timestamps, Base):
    """A tenant's "I paid" claim. Approving it is the only thing that moves a subscription."""

    __tablename__ = "payment_requests"
    __table_args__ = (
        Index(
            "uq_payment_requests_live_txn",
            "channel_kind",
            "transaction_id",
            unique=True,
            postgresql_where=text("status IN ('pending','approved')"),
        ),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=False), unique=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"), nullable=True)
    plan_name: Mapped[str] = mapped_column(String(80))
    billing_cycle: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column()  # priced from the plan at submission and frozen
    currency: Mapped[str] = mapped_column(String(3), default="BDT")
    channel_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payment_channels.id", ondelete="SET NULL"), nullable=True)
    channel_kind: Mapped[str] = mapped_column(String(24))
    channel_label: Mapped[str] = mapped_column(String(120))
    sender_account: Mapped[str] = mapped_column(String(64), default="")
    transaction_id: Mapped[str] = mapped_column(String(80))
    paid_at: Mapped[datetime | None] = mapped_column(nullable=True)
    note: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|rejected|cancelled
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    reviewed_by_name: Mapped[str] = mapped_column(String(160), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    review_note: Mapped[str] = mapped_column(String(500), default="")
    applied_period_end: Mapped[datetime | None] = mapped_column(nullable=True)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)


class SubscriptionInvoice(UUIDPK, Timestamps, Base):
    __tablename__ = "subscription_invoices"

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=False), unique=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    tenant_name: Mapped[str] = mapped_column(String(160))
    plan_name: Mapped[str] = mapped_column(String(80))
    billing_cycle: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column()
    currency: Mapped[str] = mapped_column(String(3), default="BDT")
    period_start: Mapped[datetime] = mapped_column()
    period_end: Mapped[datetime] = mapped_column()
    method: Mapped[str] = mapped_column(String(120), default="")
    reference: Mapped[str] = mapped_column(String(80), default="")
    request_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)


class AuditLog(UUIDPK, Base):
    """Append-only. There is no update or delete endpoint for this table, ever."""

    __tablename__ = "audit_log"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    actor_name: Mapped[str] = mapped_column(String(160), default="")
    actor_role: Mapped[str] = mapped_column(String(32), default="")
    impersonated_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str] = mapped_column(String(48), default="")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    summary: Mapped[str] = mapped_column(String(500), default="")
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(index=True)
