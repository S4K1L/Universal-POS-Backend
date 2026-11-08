"""Self-service signup: a request waits for a super admin, who approves it (assigning a plan) or rejects it.

Nothing is provisioned until approval — a request is just a row, so a stranger typing into the public form can
never create tenants, trials or load on the platform.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.security import hash_secret
from app.models.identity import User
from app.models.platform import BusinessType, SignupRequest
from app.schemas.tenant import TenantCreateIn
from app.services import provisioning
from app.services.common import audit, utcnow


def request_out(r: SignupRequest, *, business_type_label: str | None = None) -> dict:
    return {
        "id": str(r.id), "phone": r.phone, "business_name": r.business_name, "business_type_key": r.business_type_key,
        "business_type_label": business_type_label or r.business_type_key, "language": r.language, "status": r.status,
        "reject_reason": r.reject_reason, "plan_key": r.plan_key, "reviewed_by_name": r.reviewed_by_name,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None, "created_at": r.created_at.isoformat(),
        "tenant_id": str(r.tenant_id) if r.tenant_id else None,
    }


async def create_request(session: AsyncSession, *, phone: str, password: str, business_name: str, business_type_key: str, language: str) -> SignupRequest:
    bt = (await session.execute(select(BusinessType).where(BusinessType.key == business_type_key, BusinessType.active.is_(True)))).scalar_one_or_none()
    if bt is None:
        raise NotFoundError("Choose one of the listed business types", code="unknown_business_type")
    if (await session.execute(select(User.id).where(User.phone == phone))).first():
        raise ConflictError("That phone number already has an account — sign in instead", code="phone_taken")
    pending = (await session.execute(select(SignupRequest.id).where(SignupRequest.phone == phone, SignupRequest.status == "pending"))).first()
    if pending:
        raise ConflictError("A request for this number is already waiting for approval — sign in to see its status", code="signup_pending")
    req = SignupRequest(phone=phone, password_hash=hash_secret(password), business_name=business_name.strip(), business_type_key=bt.key, language=language)
    session.add(req)
    await session.flush()
    await audit(session, action="signup.requested", actor=None, tenant_id=None, entity="signup_request", entity_id=req.id,
                summary=f"{req.business_name} ({bt.key}) — {phone}")
    return req


async def approve(session: AsyncSession, *, admin: User, request_id: uuid.UUID, plan_key: str, billing_cycle: str,
                  trial_days: int | None, sample_data: bool) -> SignupRequest:
    req = (await session.execute(select(SignupRequest).where(SignupRequest.id == request_id).with_for_update())).scalar_one_or_none()
    if req is None:
        raise NotFoundError("Signup request not found")
    if req.status != "pending":
        raise ConflictError(f"This request was already {req.status}", code="already_reviewed")
    payload = TenantCreateIn(
        name=req.business_name, owner_name=req.business_name, phone=req.phone, business_type_key=req.business_type_key,
        plan_key=plan_key, billing_cycle=billing_cycle, trial_days=trial_days, language=req.language, sample_data=sample_data,
    )
    tenant, _owner, _ = await provisioning.provision_tenant(session, admin=admin, payload=payload, password_hash=req.password_hash)
    req.status, req.plan_key, req.tenant_id = "approved", plan_key, tenant.id
    req.reviewed_by_name, req.reviewed_at = admin.name, utcnow()
    await audit(session, action="signup.approved", actor=admin, tenant_id=None, entity="signup_request", entity_id=req.id,
                summary=f"{req.business_name}: assigned {plan_key}", data={"plan": plan_key, "tenant_id": str(tenant.id)})
    return req


async def reject(session: AsyncSession, *, admin: User, request_id: uuid.UUID, reason: str) -> SignupRequest:
    req = (await session.execute(select(SignupRequest).where(SignupRequest.id == request_id).with_for_update())).scalar_one_or_none()
    if req is None:
        raise NotFoundError("Signup request not found")
    if req.status != "pending":
        raise ConflictError(f"This request was already {req.status}", code="already_reviewed")
    req.status, req.reject_reason = "rejected", reason.strip()
    req.reviewed_by_name, req.reviewed_at = admin.name, utcnow()
    await audit(session, action="signup.rejected", actor=admin, tenant_id=None, entity="signup_request", entity_id=req.id,
                summary=f"{req.business_name}: {req.reject_reason or 'no reason given'}")
    return req
