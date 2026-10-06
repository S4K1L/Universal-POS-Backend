"""Change capture for offline sync.

Devices pull "what changed since cursor N". Rather than trusting every service to write that
log, a `before_flush` hook inspects the unit of work and records which rows are about to change
(deduplicated, delete beating upsert); an `after_flush` hook then appends the `sync_changes` rows
with a plain INSERT, once the rows they refer to exist. Bulk `UPDATE`/`DELETE` statements bypass the
ORM, so syncable tables are only ever mutated through ORM objects.
"""
import uuid

from sqlalchemy import event, insert
from sqlalchemy.orm import Session

from app.core.ids import uuid7
from app.db.mixins import SyncTracked

_KEY = "_sync_pending"


@event.listens_for(Session, "before_flush")
def _collect(session: Session, flush_context, instances) -> None:
    seen: dict[tuple[uuid.UUID, str, uuid.UUID], tuple[str, uuid.UUID | None]] = {}

    def note(obj: SyncTracked, op: str) -> None:
        tenant_id = obj.sync_tenant()
        if tenant_id is None:
            return
        entity, entity_id = obj.sync_target()
        if entity_id is None:
            return
        if obj.__sync_child__:
            op = "upsert"  # a child never deletes its parent
        key = (tenant_id, entity, entity_id)
        previous = seen.get(key)
        if previous and previous[0] == "delete":
            return
        seen[key] = (op, obj.sync_branch())

    for obj in session.new:
        if isinstance(obj, SyncTracked):
            if hasattr(obj, "id") and getattr(obj, "id", None) is None:
                obj.id = uuid7()
            note(obj, "upsert")
    for obj in session.dirty:
        if isinstance(obj, SyncTracked) and session.is_modified(obj, include_collections=False):
            note(obj, "upsert")
    for obj in session.deleted:
        if isinstance(obj, SyncTracked):
            note(obj, "delete")

    if seen:
        pending = session.info.setdefault(_KEY, [])
        pending.extend(
            {"tenant_id": t, "entity": e, "entity_id": i, "op": op, "branch_id": b} for (t, e, i), (op, b) in seen.items()
        )


@event.listens_for(Session, "after_flush")
def _write(session: Session, flush_context) -> None:
    from app.models.sync import SyncChange

    pending = session.info.pop(_KEY, None)
    if pending:
        session.execute(insert(SyncChange.__table__), pending)


@event.listens_for(Session, "after_soft_rollback")
def _discard(session: Session, previous_transaction) -> None:
    session.info.pop(_KEY, None)
