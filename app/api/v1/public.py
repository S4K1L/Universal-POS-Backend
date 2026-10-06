from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.capabilities import CAPABILITIES, FEATURES, LIMIT_KEYS
from app.db.session import get_db
from app.models.platform import BusinessType, Plan
from app.services import subscription

router = APIRouter(prefix="/public", tags=["Public"])


def plan_out(p: Plan) -> dict:
    return {
        "key": p.key, "name": p.name, "tagline": p.tagline, "monthly_price": str(p.monthly_price), "yearly_price": str(p.yearly_price),
        "currency": p.currency, "trial_days": p.trial_days, "limits": p.limits, "features": p.features,
        "capabilities": p.capabilities, "highlight": p.highlight,
    }


@router.get("/plans", summary="Plans and prices exactly as the super admin has set them")
async def plans(db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(Plan).where(Plan.active.is_(True), Plan.public.is_(True)).order_by(Plan.sort_order, Plan.monthly_price))).scalars().all()
    return {"plans": [plan_out(p) for p in rows], "features": FEATURES, "limit_keys": LIMIT_KEYS}


@router.get("/business-types", summary="The business types a shop can be set up as")
async def business_types(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(BusinessType).where(BusinessType.active.is_(True)).order_by(BusinessType.sort_order))).scalars().all()
    return [{"key": b.key, "label": b.label, "description": b.description, "family": b.family,
             "theme": (b.definition or {}).get("theme", {}), "capabilities": (b.definition or {}).get("capabilities", [])} for b in rows]


@router.get("/status", summary="Is the platform in maintenance? (the app polls this)")
async def status_(db: AsyncSession = Depends(get_db)) -> dict:
    m = await subscription.platform_setting(db, "maintenance")
    support = await subscription.platform_setting(db, "support")
    return {"maintenance": bool(m.get("enabled")), "message": m.get("message", ""), "support": support}
