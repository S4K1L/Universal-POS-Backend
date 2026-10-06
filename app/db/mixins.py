import uuid
from datetime import datetime
from typing import ClassVar

from sqlalchemy import ForeignKey, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.core.ids import uuid7


class UUIDPK:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class TenantScoped:
    """Every tenant-owned table carries this. Every query on it filters by the tenant in the
    authenticated token — never by a client-supplied value."""

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)


class SyncTracked:
    """Marks a row that devices pull. Any insert/update/delete is logged to `sync_changes`
    by the before_flush hook in `app.db.sync_hooks` — services cannot forget to."""

    __sync_entity__: ClassVar[str]
    # A child row (variant, barcode, branch…) re-publishes its parent instead of itself.
    __sync_child__: ClassVar[bool] = False

    def sync_target(self) -> tuple[str, uuid.UUID]:
        return self.__sync_entity__, self.id  # type: ignore[attr-defined]

    def sync_tenant(self) -> uuid.UUID | None:
        return getattr(self, "tenant_id", None)

    def sync_branch(self) -> uuid.UUID | None:
        return None
