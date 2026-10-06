"""Provisioning: how a business type becomes a working tenant.

One transaction creates the tenant, its first owner, a main branch, and copies the business type's
defaults (categories, tax classes, attributes, units, payment methods, capabilities) into the tenant.
Copying — rather than reading the template live — means a template edit never silently changes a
shop that is already trading; the console offers "apply update" instead.
"""
import copy
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.security import hash_secret
from app.models.catalogue import Category, TaxClass
from app.models.identity import User
from app.models.platform import BusinessType, Plan
from app.models.tenant import Branch, Tenant
from app.schemas.tenant import TenantCreateIn
from app.seeds.business_types import PAYMENT_METHODS
from app.services.common import add_months, audit, period_length, utcnow

_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_password(length: int = 10) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def _template_settings(definition: dict, *, language: str, base: dict | None = None) -> dict:
    """Template-owned keys. Tenant-added attributes (source == "custom") survive an update."""
    settings = copy.deepcopy(base or {})
    d = definition
    keep_custom = [a for a in settings.get("attributes", []) if a.get("source") == "custom"]
    settings["attributes"] = [{**a, "source": "template"} for a in copy.deepcopy(d.get("attributes", []))] + keep_custom
    settings["variant_axes"] = copy.deepcopy(d.get("variant_axes", []))
    settings["terminology"] = copy.deepcopy(d.get("terminology", {}))
    settings["units"] = copy.deepcopy(d.get("units", []))
    stock = copy.deepcopy(d.get("stock", {}))
    if base and "allow_negative" in (base.get("stock") or {}):
        stock["allow_negative"] = base["stock"]["allow_negative"]  # a shop's own choice outlives a template update
    settings["stock"] = stock
    tax = d.get("tax", {})
    settings["tax"] = {
        "mode": (settings.get("tax", {}).get("mode") if base else None) or tax.get("mode", "inclusive"),
        "cash_rounding": (settings.get("tax", {}).get("cash_rounding") if base else None) or tax.get("cash_rounding", "none"),
    }
    settings["pos"] = copy.deepcopy(d.get("pos", {}))
    settings["loyalty"] = settings.get("loyalty") if base and settings.get("loyalty") else copy.deepcopy(d.get("loyalty", {"enabled": False, "points_per_100": 0}))
    settings["theme"] = copy.deepcopy(d.get("theme", {}))
    settings["dashboard"] = copy.deepcopy(d.get("dashboard", []))
    settings["reports"] = copy.deepcopy(d.get("reports", []))
    settings["documents"] = copy.deepcopy(d.get("documents", {}))
    if "deposit_percent_min" in d:
        settings.setdefault("deposit_percent_min", d["deposit_percent_min"])
    if not base:
        settings.update({
            "currency": "BDT", "currency_symbol": "৳", "timezone": "Asia/Dhaka", "language": language,
            "payment_methods": copy.deepcopy(PAYMENT_METHODS),
            "receipt": {"header": "", "footer": "Thank you for shopping with us! ধন্যবাদ", "vat_reg_no": "", "show_tax": True},
            "scale": {"prefix": "20", "mode": "weight"},
        })
    return settings


async def provision_tenant(session: AsyncSession, *, admin: User | None, payload: TenantCreateIn) -> tuple[Tenant, User, str]:
    bt = (await session.execute(select(BusinessType).where(BusinessType.key == payload.business_type_key, BusinessType.active.is_(True)))).scalar_one_or_none()
    if bt is None:
        raise NotFoundError(f"Unknown business type: {payload.business_type_key}")
    plan = (await session.execute(select(Plan).where(Plan.key == payload.plan_key, Plan.active.is_(True)))).scalar_one_or_none()
    if plan is None:
        raise NotFoundError(f"Unknown plan: {payload.plan_key}")
    if (await session.execute(select(User.id).where(User.phone == payload.phone))).first():
        raise ConflictError("That phone number already has an account", code="phone_taken")

    now = utcnow()
    trial_days = plan.trial_days if payload.trial_days is None else payload.trial_days
    if trial_days > 0:
        status, period_end = "trialing", now + timedelta(days=trial_days)
    else:
        status = "active"
        period_end = now + period_length(payload.billing_cycle)

    definition = bt.definition or {}
    settings = _template_settings(definition, language=payload.language)
    settings["receipt"]["header"] = payload.name
    settings["receipt"]["address"] = payload.address
    settings["receipt"]["phone"] = payload.phone

    tenant = Tenant(
        name=payload.name.strip(), owner_name=payload.owner_name.strip(), phone=payload.phone, email=payload.email,
        address=payload.address, city=payload.city, business_type_id=bt.id, template_version=bt.version, plan_id=plan.id,
        status=status, billing_cycle=payload.billing_cycle, period_end=period_end, settings=settings,
        capability_overrides={"base": list(definition.get("capabilities", [])), "enabled": [], "disabled": []},
        last_active_at=now,
    )
    session.add(tenant)
    await session.flush()

    session.add(Branch(tenant_id=tenant.id, name=payload.branch_name, code="MAIN", is_main=True, address=payload.address, phone=payload.phone))

    for i, name in enumerate(definition.get("categories", [])):
        session.add(Category(tenant_id=tenant.id, name=name, sort_order=i))
    tax_classes = definition.get("tax", {}).get("classes", [{"name": "No VAT", "rate": 0, "default": True}])
    for tc in tax_classes:
        session.add(TaxClass(tenant_id=tenant.id, name=tc["name"], rate=tc["rate"], is_default=bool(tc.get("default"))))

    password = payload.password or generate_password()
    owner = User(
        tenant_id=tenant.id, name=payload.owner_name.strip(), phone=payload.phone, email=payload.email,
        password_hash=hash_secret(password), role="owner",
    )
    session.add(owner)
    await session.flush()

    await audit(session, action="tenant.created", actor=admin, tenant_id=None, entity="tenant", entity_id=tenant.id,
                summary=f"{tenant.name} ({bt.key}, {plan.key}, {status})",
                data={"business_type": bt.key, "plan": plan.key, "trial_days": trial_days})

    if payload.sample_data:
        from app.seeds.sample_catalogs import load_sample_catalog

        await load_sample_catalog(session, tenant=tenant, owner=owner)
    return tenant, owner, password


async def apply_template_update(session: AsyncSession, tenant: Tenant, actor: User | None) -> Tenant:
    """Bring a tenant up to its business type's current version, keeping its own edits."""
    bt = await session.get(BusinessType, tenant.business_type_id)
    if bt is None:
        raise NotFoundError("Business type not found")
    definition = bt.definition or {}
    tenant.settings = _template_settings(definition, language=(tenant.settings or {}).get("language", "bn"), base=tenant.settings)
    overrides = dict(tenant.capability_overrides or {})
    overrides["base"] = list(definition.get("capabilities", []))
    tenant.capability_overrides = overrides
    tenant.template_version = bt.version
    await audit(session, action="tenant.template_applied", actor=actor, tenant_id=None, entity="tenant", entity_id=tenant.id,
                summary=f"{tenant.name} → {bt.key} v{bt.version}")
    return tenant
