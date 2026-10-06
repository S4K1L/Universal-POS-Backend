"""Plan state, feature gate, usage limits.

The money rule is deliberate: a device's already-completed sales are *facts*, so the sync endpoint
never refuses them on subscription grounds. Gating happens at the edges that can say no safely —
the app locks the till when its cached subscription lapses, and online write endpoints return 402.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.capabilities import resolve_capabilities
from app.core.errors import FeatureNotInPlan, PermissionDenied, PlanLimitExceeded, SubscriptionLapsed
from app.models.catalogue import Product
from app.models.identity import User
from app.models.platform import PlatformSetting, Plan
from app.models.sales import Sale
from app.models.tenant import Branch, Device, Tenant
from app.services.common import local_month_start, utcnow

DEFAULT_PLATFORM_SETTINGS = {
    "grace_days": {"days": 3},
    "support": {"phone": "", "email": "", "whatsapp": ""},
    "billing": {"currency": "BDT", "yearly_discount_note": ""},
}


async def platform_setting(session: AsyncSession, key: str) -> dict:
    row = await session.get(PlatformSetting, key)
    return row.value if row else DEFAULT_PLATFORM_SETTINGS.get(key, {})


async def grace_days(session: AsyncSession) -> int:
    return int((await platform_setting(session, "grace_days")).get("days", 3))


@dataclass
class SubscriptionState:
    state: str  # trialing | active | grace | expired | suspended | cancelled
    period_end: datetime
    valid_until: datetime
    days_left: int
    lapsed: bool  # selling must stop (expired, suspended or cancelled)

    @property
    def blocks_writes(self) -> bool:
        return self.lapsed


def subscription_state(tenant: Tenant, grace: int, now: datetime | None = None) -> SubscriptionState:
    now = now or utcnow()
    valid_until = tenant.period_end + timedelta(days=grace)
    days_left = max((tenant.period_end - now).days, 0) if now <= tenant.period_end else 0
    if tenant.status in ("suspended", "cancelled"):
        return SubscriptionState(tenant.status, tenant.period_end, valid_until, 0, True)
    if now > valid_until:
        return SubscriptionState("expired", tenant.period_end, valid_until, 0, True)
    if now > tenant.period_end:
        return SubscriptionState("grace", tenant.period_end, valid_until, 0, False)
    return SubscriptionState(tenant.status, tenant.period_end, valid_until, days_left, False)


async def get_plan(session: AsyncSession, tenant: Tenant) -> Plan:
    plan = await session.get(Plan, tenant.plan_id)
    assert plan is not None
    return plan


def plan_has_feature(plan: Plan, feature: str) -> bool:
    return feature in (plan.features or [])


async def assert_feature(session: AsyncSession, tenant: Tenant, feature: str) -> None:
    plan = await get_plan(session, tenant)
    if not plan_has_feature(plan, feature):
        raise FeatureNotInPlan(f"Your {plan.name} plan does not include: {feature}", extra={"feature": feature})


async def assert_writable(session: AsyncSession, tenant: Tenant) -> None:
    state = subscription_state(tenant, await grace_days(session))
    if state.lapsed:
        code = "account_suspended" if state.state in ("suspended", "cancelled") else "subscription_lapsed"
        raise SubscriptionLapsed(
            "This account is not active. Renew the subscription to continue." if code == "subscription_lapsed"
            else "This account has been suspended. Contact support.",
            code=code,
        )


def effective_capabilities(tenant: Tenant, plan: Plan) -> list[str]:
    ov = tenant.capability_overrides or {}
    return resolve_capabilities(
        template=ov.get("base", []),
        enabled=ov.get("enabled"),
        disabled=ov.get("disabled"),
        plan_allowed=plan.capabilities,
    )


def has_capability(tenant: Tenant, plan: Plan, capability: str) -> bool:
    return capability in effective_capabilities(tenant, plan)


# ---------------------------------------------------------------- usage & limits


async def usage(session: AsyncSession, tenant: Tenant) -> dict[str, int]:
    tid = tenant.id

    async def count(stmt):
        return int((await session.execute(stmt)).scalar_one())

    month_start = local_month_start(tenant)
    return {
        "products": await count(select(func.count()).select_from(Product).where(Product.tenant_id == tid, Product.active.is_(True))),
        "staff": await count(select(func.count()).select_from(User).where(User.tenant_id == tid, User.active.is_(True))),
        "branches": await count(select(func.count()).select_from(Branch).where(Branch.tenant_id == tid, Branch.active.is_(True))),
        "devices": await count(select(func.count()).select_from(Device).where(Device.tenant_id == tid, Device.active.is_(True))),
        "sales_per_month": await count(
            select(func.count()).select_from(Sale).where(Sale.tenant_id == tid, Sale.sold_at >= month_start, Sale.status == "completed")
        ),
    }


def over_limit(limit: int, used: int) -> bool:
    return limit >= 0 and used >= limit


async def assert_within_limit(session: AsyncSession, tenant: Tenant, key: str, adding: int = 1) -> None:
    plan = await get_plan(session, tenant)
    limit = int((plan.limits or {}).get(key, -1))
    if limit < 0:
        return
    used = (await usage(session, tenant))[key]
    if used + adding > limit:
        raise PlanLimitExceeded(
            f"Your {plan.name} plan allows {limit} {key.replace('_', ' ')}. Upgrade to add more.",
            extra={"limit_key": key, "limit": limit, "used": used},
        )


def require_permission_for(user: User, permission: str) -> None:
    from app.core.permissions import effective_permissions

    if permission not in {p.value for p in effective_permissions(user.role, user.permissions)}:
        raise PermissionDenied(f"Missing permission: {permission}", code="permission_denied")
