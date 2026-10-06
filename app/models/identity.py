import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import SyncTracked, Timestamps, UUIDPK


class User(UUIDPK, Timestamps, SyncTracked, Base):
    __tablename__ = "users"
    __sync_entity__ = "context"
    __sync_child__ = True

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(24))
    active: Mapped[bool] = mapped_column(default=True)
    # None => inherit the role's permissions. [] => explicitly nothing.
    permissions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # None => every branch. A list (even empty) confines the account to those branches.
    branch_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)

    def sync_target(self) -> tuple[str, uuid.UUID]:
        return "context", self.tenant_id  # type: ignore[return-value]


class RefreshToken(UUIDPK, Timestamps, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column()
    revoked: Mapped[bool] = mapped_column(default=False)
