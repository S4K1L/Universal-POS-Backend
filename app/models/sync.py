import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TenantScoped, UUIDPK


class SyncEvent(UUIDPK, TenantScoped, Base):
    """One applied device event. The unique (tenant, event_id) is what makes a retried push harmless."""

    __tablename__ = "sync_events"
    __table_args__ = (UniqueConstraint("tenant_id", "event_id", name="uq_sync_events_tenant_event"),)

    event_id: Mapped[uuid.UUID] = mapped_column()
    type: Mapped[str] = mapped_column(String(32))
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    applied_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SyncChange(Base):
    """What changed, in order. Devices pull `seq > cursor` and re-read the current row."""

    __tablename__ = "sync_changes"
    __table_args__ = (Index("ix_sync_changes_tenant_seq", "tenant_id", "seq"),)

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    entity: Mapped[str] = mapped_column(String(24))
    entity_id: Mapped[uuid.UUID] = mapped_column()
    op: Mapped[str] = mapped_column(String(8), default="upsert")
    branch_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
