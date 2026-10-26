import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import CapabilityDisabled, FeatureNotInPlan, PermissionDenied, PlatformMaintenance, SubscriptionLapsed
from app.core.permissions import Permission, effective_permissions
from app.core.security import decode_token
from app.db.session import get_db
from app.models.identity import User
from app.models.platform import Plan
from app.models.tenant import Tenant
from app.services import subscription
from app.services.common import utcnow

bearer = HTTPBearer(auto_error=False, description="Paste the access_token returned by POST /api/v1/auth/login")


@dataclass
class Claims:
    user: User
    impersonated_by: uuid.UUID | None


async def current_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: AsyncSession = Depends(get_db)
) -> Claims:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}) from exc
    if payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not an access token")
    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    imp = payload.get("impersonated_by")
    return Claims(user, uuid.UUID(imp) if imp else None)


async def current_user(claims: Claims = Depends(current_claims)) -> User:
    return claims.user


def require_super_admin(user: User = Depends(current_user)) -> User:
    if user.role != "super_admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform admin access required")
    return user


@dataclass
class Ctx:
    user: User
    tenant: Tenant
    plan: Plan
    impersonated_by: uuid.UUID | None = None

    def can(self, permission: str) -> bool:
        return permission in {p.value for p in effective_permissions(self.user.role, self.user.permissions)}

    def has_capability(self, capability: str) -> bool:
        return capability in subscription.effective_capabilities(self.tenant, self.plan)


def tenant_ctx(
    *,
    permission: Permission | None = None,
    any_permission: tuple[Permission, ...] = (),
    feature: str | None = None,
    capability: str | None = None,
    write: bool = False,
    allow_lapsed: bool = False,
    allow_suspended: bool = False,
) -> Callable:
    """The three gates, plus subscription state, as one dependency.

    Plan (feature) × Capability (business type) × Role (permission) must all pass. A lapsed
    account can still *read*; writes return 402 — except billing, which has to work for it to recover.
    """

    async def dependency(claims: Claims = Depends(current_claims), db: AsyncSession = Depends(get_db)) -> Ctx:
        user = claims.user
        if user.tenant_id is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has no business workspace")
        tenant = await db.get(Tenant, user.tenant_id)
        if tenant is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Business not found")
        plan = await subscription.get_plan(db, tenant)
        ctx = Ctx(user, tenant, plan, claims.impersonated_by)
        db.info["impersonated_by"] = claims.impersonated_by  # read by services.common.audit, so no call site can forget it

        state = subscription.subscription_state(tenant, await subscription.grace_days(db))
        if state.state in ("suspended", "cancelled") and not allow_suspended:
            raise SubscriptionLapsed("This account has been suspended. Contact support.", code="account_suspended")
        if write and state.lapsed and not allow_lapsed:
            raise SubscriptionLapsed("Your subscription has expired. Renew to continue.", code="subscription_lapsed")

        if permission and not ctx.can(permission.value):
            raise PermissionDenied(f"Missing permission: {permission.value}")
        if any_permission and not any(ctx.can(p.value) for p in any_permission):
            raise PermissionDenied("Missing permission: " + " or ".join(p.value for p in any_permission))
        if feature and feature not in (plan.features or []):
            raise FeatureNotInPlan(f"Your {plan.name} plan does not include: {feature}", extra={"feature": feature})
        if capability and not ctx.has_capability(capability):
            raise CapabilityDisabled(f"This business does not use: {capability}", extra={"capability": capability})

        # debounced "last active" — once every few minutes is plenty
        now = utcnow()
        if tenant.last_active_at is None or (now - tenant.last_active_at).total_seconds() > 300:
            tenant.last_active_at = now
            await db.flush()
        return ctx

    return dependency


async def maintenance_gate(request: Request, db: AsyncSession = Depends(get_db)) -> None:
    """While the platform maintenance switch is on, every route (bar the exemptions) answers 503."""
    exempt = ("/health", "/docs", "/openapi.json", "/redoc", "/api/v1/auth", "/api/v1/admin", "/api/v1/public")
    if request.url.path.startswith(exempt):
        return
    setting = await subscription.platform_setting(db, "maintenance")
    if setting.get("enabled"):
        raise PlatformMaintenance(setting.get("message") or "Scheduled maintenance — back shortly.")
