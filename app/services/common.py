import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.platform import AuditLog
from app.models.tenant import TenantCounter


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def tenant_zone(tenant) -> ZoneInfo:
    name = (tenant.settings or {}).get("timezone", "Asia/Dhaka")
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Asia/Dhaka")


def local_day_start(tenant, when: datetime | None = None) -> datetime:
    """Midnight in the shop's own timezone, as UTC — 'today' means the shop's today."""
    zone = tenant_zone(tenant)
    local = (when or utcnow()).astimezone(zone)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def local_month_start(tenant, when: datetime | None = None) -> datetime:
    zone = tenant_zone(tenant)
    local = (when or utcnow()).astimezone(zone)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def next_counter(session: AsyncSession, *, tenant_id: uuid.UUID, name: str) -> int:
    """Atomic per-tenant counter (single UPSERT … RETURNING), safe under concurrent requests."""
    stmt = (
        pg_insert(TenantCounter)
        .values(tenant_id=tenant_id, name=name, value=1)
        .on_conflict_do_update(
            index_elements=[TenantCounter.tenant_id, TenantCounter.name],
            set_={"value": TenantCounter.value + 1},
        )
        .returning(TenantCounter.value)
    )
    return (await session.execute(stmt)).scalar_one()


async def next_number(session: AsyncSession, *, tenant_id: uuid.UUID, name: str, prefix: str, width: int = 6) -> str:
    value = await next_counter(session, tenant_id=tenant_id, name=name)
    return f"{prefix}-{value:0{width}d}"


async def audit(
    session: AsyncSession,
    *,
    action: str,
    actor=None,
    tenant_id: uuid.UUID | None = None,
    entity: str = "",
    entity_id: str | uuid.UUID = "",
    summary: str = "",
    data: dict | None = None,
    impersonated_by: uuid.UUID | None = None,
    at: datetime | None = None,
) -> None:
    """Write-once audit row, in the same transaction as the change it describes."""
    session.add(
        AuditLog(
            tenant_id=tenant_id if tenant_id is not None else getattr(actor, "tenant_id", None),
            actor_id=getattr(actor, "id", None),
            actor_name=getattr(actor, "name", "") or "system",
            actor_role=getattr(actor, "role", ""),
            impersonated_by=impersonated_by,
            action=action,
            entity=entity,
            entity_id=str(entity_id),
            summary=summary[:500],
            data=data or {},
            at=at or utcnow(),
        )
    )


def add_months(start: datetime, months: int) -> datetime:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return start.replace(year=year, month=month, day=day)


def period_length(cycle: str) -> timedelta:
    return timedelta(days=365 if cycle == "yearly" else 30)
