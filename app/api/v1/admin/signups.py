import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_super_admin
from app.db.session import get_db
from app.models.identity import User
from app.models.platform import BusinessType, SignupRequest
from app.schemas.common import Strict
from app.services import signups as signup_service

router = APIRouter(prefix="/admin/signups", tags=["Platform Console — Businesses"], dependencies=[Depends(require_super_admin)])


class ApproveIn(Strict):
    plan_key: str = Field(description="The package to assign — the business starts on it")
    billing_cycle: str = Field(default="monthly", pattern="^(monthly|yearly)$")
    trial_days: int | None = Field(default=None, ge=0, le=365, description="Leave empty to use the plan's own free-trial days")
    sample_data: bool = False


class RejectIn(Strict):
    reason: str = Field(default="", max_length=300)


@router.get("", summary="Signup requests, newest first — what the Approvals page lists")
async def list_requests(status: str = Query(default="pending", pattern="^(pending|approved|rejected|all)$"), limit: int = Query(default=100, ge=1, le=200),
                        db: AsyncSession = Depends(get_db)) -> dict:
    q = select(SignupRequest).order_by(SignupRequest.created_at.desc()).limit(limit)
    if status != "all":
        q = q.where(SignupRequest.status == status)
    rows = (await db.execute(q)).scalars().all()
    labels = {b.key: b.label for b in (await db.execute(select(BusinessType))).scalars()}
    counts = dict((await db.execute(select(SignupRequest.status, func.count()).group_by(SignupRequest.status))).all())
    return {"items": [signup_service.request_out(r, business_type_label=labels.get(r.business_type_key)) for r in rows],
            "counts": {k: int(counts.get(k, 0)) for k in ("pending", "approved", "rejected")}}


@router.post("/{request_id}/approve", summary="Approve, assign a package, and open the business")
async def approve(request_id: uuid.UUID, body: ApproveIn, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    req = await signup_service.approve(db, admin=admin, request_id=request_id, plan_key=body.plan_key, billing_cycle=body.billing_cycle,
                                       trial_days=body.trial_days, sample_data=body.sample_data)
    await db.commit()
    return signup_service.request_out(req)


@router.post("/{request_id}/reject", summary="Decline a request, with a reason the applicant will see")
async def reject(request_id: uuid.UUID, body: RejectIn, admin: User = Depends(require_super_admin), db: AsyncSession = Depends(get_db)) -> dict:
    req = await signup_service.reject(db, admin=admin, request_id=request_id, reason=body.reason)
    await db.commit()
    return signup_service.request_out(req)
