"""Plans & prices, business types, capabilities, platform settings — everything the super admin can configure."""
import re
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_super_admin
from app.api.v1.public import plan_out
from app.core.capabilities import CAPABILITIES, FEATURES, LIMIT_KEYS, resolve_capabilities
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.money import Money
from app.db.session import get_db
from app.models.identity import User
from app.models.platform import BusinessType, Plan, PlatformSetting
from app.models.tenant import Tenant
from app.schemas.common import Strict
from app.services import subscription
from app.services.common import audit, utcnow

router = APIRouter(prefix="/admin", tags=["Platform Console — Configuration"], dependencies=[Depends(require_super_admin)])


# ------------------------------------------------------------------------------------------------ plans


class PlanIn(Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,38}$")
    name: str = Field(min_length=1, max_length=80)
    tagline: str = Field(default="", max_length=200)
    monthly_price: Money = Field(ge=0)
    yearly_price: Money = Field(ge=0)
    currency: str = Field(default="BDT", min_length=3, max_length=3)
    trial_days: int = Field(default=0, ge=0, le=365)
    limits: dict[str, int] = Field(default_factory=dict)
    features: list[str] = Field(default_factory=list)
    capabilities: list[str] | None = None
    highlight: bool = False
    public: bool = True
    active: bool = True
    sort_order: int = 0

    @field_validator("limits")
    @classmethod
    def _limits(cls, v: dict[str, int]) -> dict[str, int]:
        bad = [k for k in v if k not in LIMIT_KEYS]
        if bad:
            raise ValueError(f"unknown limit: {bad[0]}")
        if any(x < -1 for x in v.values()):
            raise ValueError("limits are -1 (unlimited) or zero and above")
        return v

    @field_validator("features")
    @classmethod
    def _features(cls, v: list[str]) -> list[str]:
        bad = [k for k in v if k not in FEATURES]
        if bad:
            raise ValueError(f"unknown feature: {bad[0]}")
        return sorted(set(v))

    @field_validator("capabilities")
    @classmethod
    def _caps(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        bad = [k for k in v if k not in CAPABILITIES]
        if bad:
            raise ValueError(f"unknown capability: {bad[0]}")
        return sorted(set(v))


async def _plan_row(db: AsyncSession, p: Plan) -> dict:
    count = (await db.execute(select(func.count()).select_from(Tenant).where(Tenant.plan_id == p.id))).scalar_one()
    return {**plan_out(p), "id": str(p.id), "public": p.public, "active": p.active, "sort_order": p.sort_order, "tenant_count": int(count)}


@router.get("/plans", summary="All plans, with how many businesses are on each")
async def list_plans(db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(Plan).order_by(Plan.sort_order, Plan.monthly_price))).scalars().all()
    return {"plans": [await _plan_row(db, p) for p in rows], "features": FEATURES, "limit_keys": LIMIT_KEYS,
            "capabilities": {k: {"label": c.label, "group": c.group, "status": c.status} for k, c in CAPABILITIES.items()}}


@router.post("/plans", status_code=201, summary="Create a plan and set its price")
async def create_plan(body: PlanIn, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    if (await db.execute(select(Plan.id).where(Plan.key == body.key))).first():
        raise ConflictError("A plan with that key already exists", code="plan_key_taken")
    p = Plan(**body.model_dump())
    db.add(p)
    await db.flush()
    await audit(db, action="plan.created", actor=admin, tenant_id=None, entity="plan", entity_id=p.id,
                summary=f"{p.name}: {p.monthly_price}/mo, {p.yearly_price}/yr")
    await db.commit()
    return await _plan_row(db, p)


@router.put("/plans/{plan_id}", summary="Edit a plan — prices, limits, features")
async def update_plan(plan_id: str, body: PlanIn, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    import uuid as _uuid

    p = await db.get(Plan, _uuid.UUID(plan_id))
    if p is None:
        raise NotFoundError("Plan not found")
    if body.key != p.key and (await db.execute(select(Plan.id).where(Plan.key == body.key))).first():
        raise ConflictError("A plan with that key already exists", code="plan_key_taken")
    before = {"monthly_price": str(p.monthly_price), "yearly_price": str(p.yearly_price), "features": p.features, "limits": p.limits, "active": p.active}
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    after = {"monthly_price": str(p.monthly_price), "yearly_price": str(p.yearly_price), "features": p.features, "limits": p.limits, "active": p.active}
    await audit(db, action="plan.updated", actor=admin, tenant_id=None, entity="plan", entity_id=p.id,
                summary=f"{p.name}: {before['monthly_price']}→{after['monthly_price']}/mo, {before['yearly_price']}→{after['yearly_price']}/yr",
                data={"before": before, "after": after})
    # businesses on this plan must re-read their limits/features
    for t in (await db.execute(select(Tenant).where(Tenant.plan_id == p.id))).scalars():
        t.updated_at = utcnow()
    await db.commit()
    return await _plan_row(db, p)


@router.delete("/plans/{plan_id}")
async def delete_plan(plan_id: str, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    import uuid as _uuid

    p = await db.get(Plan, _uuid.UUID(plan_id))
    if p is None:
        raise NotFoundError("Plan not found")
    count = (await db.execute(select(func.count()).select_from(Tenant).where(Tenant.plan_id == p.id))).scalar_one()
    if count:
        p.active = False
        p.public = False
        result = "deactivated"
    else:
        await db.delete(p)
        result = "deleted"
    await audit(db, action=f"plan.{result}", actor=admin, tenant_id=None, entity="plan", entity_id=plan_id, summary=p.name)
    await db.commit()
    return {"result": result, "tenants_still_on_it": int(count)}


# ---------------------------------------------------------------------------------------- business types


class AttributeFlags(Strict):
    searchable: bool = False
    filterable: bool = False
    show_in_pos: bool = False
    show_on_label: bool = False
    show_on_receipt: bool = False


class AttributeDef(Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,38}$")
    label: str = Field(min_length=1, max_length=80)
    label_bn: str = ""
    type: str = Field(pattern="^(text|textarea|int|decimal|money|bool|date|select|multiselect|select_or_create|color)$")
    required: bool = False
    options: list[str] = Field(default_factory=list)
    unit: str | None = None
    group: str = "Details"
    flags: AttributeFlags = Field(default_factory=AttributeFlags)


class BusinessTypeDefinition(Strict):
    capabilities: list[str]
    terminology: dict[str, str] = Field(default_factory=dict)
    attributes: list[AttributeDef] = Field(default_factory=list)
    variant_axes: list[dict] = Field(default_factory=list)
    stock: dict = Field(default_factory=dict)
    units: list[dict] = Field(default_factory=list)
    tax: dict = Field(default_factory=dict)
    categories: list[str] = Field(default_factory=list)
    pos: dict = Field(default_factory=dict)
    loyalty: dict = Field(default_factory=dict)
    theme: dict = Field(default_factory=dict)
    dashboard: list[str] = Field(default_factory=list)
    reports: list[str] = Field(default_factory=list)
    documents: dict = Field(default_factory=dict)
    deposit_percent_min: int | None = None

    @field_validator("capabilities")
    @classmethod
    def _caps(cls, v: list[str]) -> list[str]:
        unknown = [k for k in v if k not in CAPABILITIES]
        if unknown:
            raise ValueError(f"unknown capability: {unknown[0]}")
        planned = [k for k in v if CAPABILITIES[k].status != "ready"]
        if planned:
            raise ValueError(f"{planned[0]} is planned but not shipped yet")
        closed = resolve_capabilities(template=v)
        missing = sorted(set(v) - set(closed))
        if missing:
            raise ValueError(f"{missing[0]} is missing something it requires ({', '.join(CAPABILITIES[missing[0]].requires)})")
        return sorted(set(v))


class BusinessTypeIn(Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,38}$")
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    family: str = Field(default="counter_retail", pattern="^(counter_retail|order_based)$")
    sort_order: int = 0
    active: bool = True
    definition: BusinessTypeDefinition


async def _bt_row(db: AsyncSession, b: BusinessType) -> dict:
    count = (await db.execute(select(func.count()).select_from(Tenant).where(Tenant.business_type_id == b.id))).scalar_one()
    stale = (await db.execute(select(func.count()).select_from(Tenant).where(Tenant.business_type_id == b.id, Tenant.template_version < b.version))).scalar_one()
    return {"id": str(b.id), "key": b.key, "label": b.label, "description": b.description, "family": b.family, "version": b.version,
            "sort_order": b.sort_order, "active": b.active, "definition": b.definition, "tenant_count": int(count), "tenants_on_older_version": int(stale)}


@router.get("/business-types")
async def list_business_types(db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(BusinessType).order_by(BusinessType.sort_order, BusinessType.label))).scalars().all()
    return {"business_types": [await _bt_row(db, b) for b in rows],
            "capabilities": {k: {"label": c.label, "description": c.description, "group": c.group, "status": c.status, "requires": list(c.requires)}
                             for k, c in CAPABILITIES.items()}}


@router.post("/business-types", status_code=201, summary="Define a new kind of business — data only, no code")
async def create_business_type(body: BusinessTypeIn, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    if (await db.execute(select(BusinessType.id).where(BusinessType.key == body.key))).first():
        raise ConflictError("A business type with that key already exists", code="key_taken")
    b = BusinessType(key=body.key, label=body.label, description=body.description, family=body.family, sort_order=body.sort_order,
                     active=body.active, version=1, definition=body.definition.model_dump(mode="json"))
    db.add(b)
    await db.flush()
    await audit(db, action="business_type.created", actor=admin, tenant_id=None, entity="business_type", entity_id=b.id, summary=b.label)
    await db.commit()
    return await _bt_row(db, b)


@router.put("/business-types/{key}", summary="Edit a business type; bumps its version (shops adopt it via “apply update”)")
async def update_business_type(key: str, body: BusinessTypeIn, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    b = (await db.execute(select(BusinessType).where(BusinessType.key == key))).scalar_one_or_none()
    if b is None:
        raise NotFoundError("Business type not found")
    if body.key != key:
        raise ValidationFailed("A business type's key can't change", code="validation_failed")
    new_def = body.definition.model_dump(mode="json")
    changed = new_def != b.definition
    b.label, b.description, b.family, b.sort_order, b.active = body.label, body.description, body.family, body.sort_order, body.active
    if changed:
        old_caps, new_caps = set(b.definition.get("capabilities", [])), set(new_def["capabilities"])
        b.definition = new_def
        b.version += 1
        await audit(db, action="business_type.updated", actor=admin, tenant_id=None, entity="business_type", entity_id=b.id,
                    summary=f"{b.label} → v{b.version}", data={"capabilities_added": sorted(new_caps - old_caps), "capabilities_removed": sorted(old_caps - new_caps)})
    await db.commit()
    return await _bt_row(db, b)


# ------------------------------------------------------------------------------------ platform settings

SETTING_KEYS = {"grace_days", "support", "maintenance", "billing"}


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)) -> dict:
    return {k: await subscription.platform_setting(db, k) for k in sorted(SETTING_KEYS)}


@router.put("/settings/{key}")
async def put_setting(key: str, body: dict, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    if key not in SETTING_KEYS:
        raise NotFoundError("Unknown setting")
    if key == "grace_days":
        days = int(body.get("days", 3))
        if not 0 <= days <= 60:
            raise ValidationFailed("Grace period must be 0–60 days", code="validation_failed")
        body = {"days": days}
    row = await db.get(PlatformSetting, key)
    if row is None:
        row = PlatformSetting(key=key)
        db.add(row)
    row.value, row.updated_at, row.updated_by = body, utcnow(), admin.name
    await audit(db, action="platform.setting", actor=admin, tenant_id=None, entity="setting", entity_id=key, summary=f"{key} changed", data=body)
    await db.commit()
    return row.value
