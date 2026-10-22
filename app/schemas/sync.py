import uuid
from datetime import datetime

from pydantic import Field

from app.schemas.common import Strict


class SyncEventIn(Strict):
    id: uuid.UUID
    type: str = Field(max_length=32)
    occurred_at: datetime
    actor_id: uuid.UUID | None = None
    payload: dict


class PushIn(Strict):
    events: list[SyncEventIn] = Field(max_length=100)
