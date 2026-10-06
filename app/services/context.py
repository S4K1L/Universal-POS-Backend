"""The tenant context: one bootstrap payload telling the app what this business is and may do."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.capabilities import CAPABILITIES
from app.core.permissions import ROLE_LABELS, effective_permissions
from app.models.identity import User
from app.models.platform import BusinessType
from app.models.tenant import Branch, Device, Tenant
from app.services import subscription
from app.services.common import utcnow

# id, label, required plan feature, required capability, required permission (any of)
MODULES = [
    ("dashboard", "Dashboard", None, None, None),
    ("pos", "Sell", "pos", None, ("sell",)),
    ("orders", "Orders", "orders", "document_flow", ("manage_orders",)),
    ("sales", "Sales", "pos", None, ("view_sales",)),
    ("products", "Products", "inventory", None, ("view_inventory",)),
    ("stock", "Stock", "inventory", None, ("view_inventory",)),
    ("purchases", "Purchases", "purchases", None, ("manage_purchases",)),
    ("customers", "Customers", "customers", None, ("manage_customers", "collect_dues")),
    ("expenses", "Expenses", "expenses", None, ("manage_expenses",)),
    ("shifts", "Shifts", None, "shifts", ("manage_shifts",)),
    ("reports", "Reports", "reports", None, ("view_reports",)),
    ("settings", "Settings", None, None, None),
    ("billing", "Subscription", None, None, ("manage_subscription",)),
]


def user_out(user: User) -> dict:
    perms = sorted(p.value for p in effective_permissions(user.role, user.permissions))
    return {
        "id": str(user.id), "name": user.name, "phone": user.phone, "email": user.email, "role": user.role,
        "role_label": ROLE_LABELS.get(user.role, user.role), "active": user.active, "tenant_id": str(user.tenant_id) if user.tenant_id else None,
        "permissions": perms, "branch_ids": user.branch_ids, "permission_overrides": user.permissions,
    }


async def build_context(session: AsyncSession, tenant: Tenant, user: User) -> dict:
    plan = await subscription.get_plan(session, tenant)
    bt = await session.get(BusinessType, tenant.business_type_id)
    caps = subscription.effective_capabilities(tenant, plan)
    grace = await subscription.grace_days(session)
    state = subscription.subscription_state(tenant, grace)
    usage = await subscription.usage(session, tenant)
    perms = {p.value for p in effective_permissions(user.role, user.permissions)}
    settings = tenant.settings or {}

    branches = (await session.execute(
        select(Branch).where(Branch.tenant_id == tenant.id, Branch.active.is_(True)).order_by(Branch.is_main.desc(), Branch.name)
    )).scalars().all()
    if user.branch_ids is not None:
        branches = [b for b in branches if str(b.id) in user.branch_ids]

    modules = []
    for mid, label, feature, capability, need in MODULES:
        reason = None
        if capability and capability not in caps:
            reason = "capability"
        elif feature and feature not in (plan.features or []):
            reason = "plan"
        elif need and not (set(need) & perms):
            reason = "permission"
        modules.append({"id": mid, "label": label, "available": reason is None, "locked_reason": reason})

    notice = None
    if state.state == "suspended":
        notice = "This account is suspended. Please contact support."
    elif state.state == "expired":
        notice = "Your subscription has expired. Renew to keep selling."
    elif state.state == "grace":
        notice = "Your subscription has lapsed — renew now to avoid losing access."
    elif state.state == "trialing":
        notice = f"Free trial · {state.days_left} day{'s' if state.days_left != 1 else ''} left"
    elif state.days_left <= 5:
        notice = f"Your {plan.name} plan renews in {state.days_left} day{'s' if state.days_left != 1 else ''}"

    return {
        "tenant": {
            "id": str(tenant.id), "name": tenant.name, "owner_name": tenant.owner_name, "phone": tenant.phone,
            "address": tenant.address, "city": tenant.city,
        },
        "business_type": {
            "key": bt.key, "label": bt.label, "family": bt.family, "version": tenant.template_version,
            "theme": settings.get("theme", {}),
        },
        "capabilities": caps,
        "capability_catalogue": {k: {"label": c.label, "group": c.group} for k, c in CAPABILITIES.items() if k in caps},
        "features": list(plan.features or []),
        "limits": plan.limits or {},
        "usage": usage,
        "modules": modules,
        "terminology": settings.get("terminology", {}),
        "attributes": settings.get("attributes", []),
        "variant_axes": settings.get("variant_axes", []),
        "units": settings.get("units", []),
        "settings": {
            "currency": settings.get("currency", "BDT"), "currency_symbol": settings.get("currency_symbol", "৳"),
            "language": settings.get("language", "bn"), "timezone": settings.get("timezone", "Asia/Dhaka"),
            "tax": settings.get("tax", {}), "stock": settings.get("stock", {}), "pos": settings.get("pos", {}),
            "loyalty": settings.get("loyalty", {}), "receipt": settings.get("receipt", {}),
            "payment_methods": settings.get("payment_methods", []), "scale": settings.get("scale", {}),
            "documents": settings.get("documents", {}), "dashboard": settings.get("dashboard", []),
            "reports": settings.get("reports", []), "deposit_percent_min": settings.get("deposit_percent_min", 0),
        },
        "subscription": {
            "plan": {"key": plan.key, "name": plan.name, "monthly_price": str(plan.monthly_price), "yearly_price": str(plan.yearly_price)},
            "state": state.state, "status": tenant.status, "billing_cycle": tenant.billing_cycle,
            "period_end": tenant.period_end.isoformat(), "valid_until": state.valid_until.isoformat(),
            "days_left": state.days_left, "lapsed": state.lapsed, "notice": notice,
        },
        "user": user_out(user),
        "branches": [{"id": str(b.id), "name": b.name, "code": b.code, "is_main": b.is_main, "address": b.address} for b in branches],
        "server_time": utcnow().isoformat(),
    }
