import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import SyncTracked, TenantScoped, Timestamps, UUIDPK


class Tenant(UUIDPK, Timestamps, SyncTracked, Base):
    __tablename__ = "tenants"
    __sync_entity__ = "context"

    name: Mapped[str] = mapped_column(String(160))
    owner_name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(32))
    email: Mapped[str] = mapped_column(String(200), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    city: Mapped[str] = mapped_column(String(120), default="")

    business_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("business_types.id", ondelete="RESTRICT"))
    template_version: Mapped[int] = mapped_column(Integer, default=1)

    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(16), default="trialing")  # trialing|active|suspended|cancelled
    billing_cycle: Mapped[str] = mapped_column(String(16), default="monthly")
    period_end: Mapped[datetime] = mapped_column()

    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    capability_overrides: Mapped[dict] = mapped_column(JSONB, default=dict)  # {"enabled": [...], "disabled": [...]}
    last_active_at: Mapped[datetime | None] = mapped_column(nullable=True)

    def sync_tenant(self) -> uuid.UUID:
        return self.id


class Branch(UUIDPK, TenantScoped, Timestamps, SyncTracked, Base):
    __tablename__ = "branches"
    __sync_entity__ = "context"
    __sync_child__ = True

    name: Mapped[str] = mapped_column(String(160))
    code: Mapped[str] = mapped_column(String(16), default="MAIN")
    address: Mapped[str] = mapped_column(String(300), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    manager_name: Mapped[str] = mapped_column(String(160), default="")
    is_main: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)

    def sync_target(self) -> tuple[str, uuid.UUID]:
        return "context", self.tenant_id


class TenantCounter(Base):
    __tablename__ = "tenant_counters"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)
