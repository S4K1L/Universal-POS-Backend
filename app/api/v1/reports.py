import csv
import io
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Ctx, tenant_ctx
from app.core.errors import ValidationFailed
from app.core.permissions import Permission
from app.db.session import get_db
from app.models.tenant import Branch
from app.services import reports as svc
from app.services.common import tenant_zone, utcnow

router = APIRouter(prefix="/reports", tags=["Reports"])
gate = tenant_ctx(permission=Permission.view_reports, feature="reports", allow_lapsed=True)


def _today(ctx: Ctx) -> date:
    return utcnow().astimezone(tenant_zone(ctx.tenant)).date()


def _scope(ctx: Ctx, branch_id: uuid.UUID | None) -> uuid.UUID | None:
    """Restricted users are pinned to a branch of theirs even if they ask for the whole business."""
    if ctx.user.branch_ids is not None:
        allowed = [uuid.UUID(b) for b in ctx.user.branch_ids]
        if branch_id and branch_id in allowed:
            return branch_id
        return allowed[0] if allowed else uuid.uuid4()
    return branch_id


def _range(ctx: Ctx, date_from: date | None, date_to: date | None) -> tuple[date, date]:
    end = date_to or _today(ctx)
    start = date_from or end
    if start > end:
        raise ValidationFailed("The start date is after the end date", code="validation_failed")
    if (end - start).days > 400:
        raise ValidationFailed("Pick a range of at most 400 days", code="validation_failed")
    return start, end


@router.get("/summary", summary="Totals, payment mix and (with permission) margin for a date range")
async def summary(date_from: date | None = None, date_to: date | None = None, branch_id: uuid.UUID | None = None,
                  ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> dict:
    start, end = _range(ctx, date_from, date_to)
    lo, hi = svc.day_range(ctx.tenant, start, end)
    return await svc.summary(db, ctx.tenant, lo, hi, _scope(ctx, branch_id), include_margin=ctx.can("view_margin"))


@router.get("/dashboard", summary="Everything the home screen shows, in one call")
async def dashboard(branch_id: uuid.UUID | None = None, days: int = Query(default=14, ge=3, le=60), ctx: Ctx = Depends(tenant_ctx(allow_lapsed=True)),
                    db: AsyncSession = Depends(get_db)) -> dict:
    today = _today(ctx)
    scope = _scope(ctx, branch_id)
    out: dict = {"date": today.isoformat()}
    if ctx.can("view_sales") or ctx.can("view_reports"):
        lo, hi = svc.day_range(ctx.tenant, today, today)
        yl, yh = svc.day_range(ctx.tenant, today - timedelta(days=1), today - timedelta(days=1))
        out["today"] = await svc.summary(db, ctx.tenant, lo, hi, scope, include_margin=ctx.can("view_margin"))
        out["yesterday"] = await svc.summary(db, ctx.tenant, yl, yh, scope, include_margin=False)
        out["trend"] = await svc.sales_by_day(db, ctx.tenant, today - timedelta(days=days - 1), today, scope)
        out["hourly"] = await svc.sales_by_hour(db, ctx.tenant, today, today, scope)
        out["top_products"] = await svc.top_products(db, ctx.tenant, *svc.day_range(ctx.tenant, today - timedelta(days=6), today), scope, limit=6)
    if ctx.can("view_inventory") and "inventory" in (ctx.plan.features or []):
        branch = scope
        if branch is None:
            from sqlalchemy import select

            branch = (await db.execute(select(Branch.id).where(Branch.tenant_id == ctx.tenant.id, Branch.is_main.is_(True)))).scalar_one()
        out["low_stock"] = await svc.low_stock(db, ctx.tenant, branch, limit=8)
        out["expiring"] = (await svc.expiring(db, ctx.tenant, scope, 60, include_cost=ctx.can("view_cost")))[:8] if ctx.has_capability("expiry_tracking") else []
    if ctx.has_capability("customer_credit") and (ctx.can("collect_dues") or ctx.can("view_reports")):
        d = await svc.dues(db, ctx.tenant)
        out["dues"] = {"total": d["total"], "count": len(d["customers"]), "top": d["customers"][:5]}
    return out


@router.get("/sales-by-day")
async def by_day(date_from: date | None = None, date_to: date | None = None, branch_id: uuid.UUID | None = None, ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> list[dict]:
    start, end = _range(ctx, date_from, date_to)
    return await svc.sales_by_day(db, ctx.tenant, start, end, _scope(ctx, branch_id))


@router.get("/top-products")
async def top(date_from: date | None = None, date_to: date | None = None, branch_id: uuid.UUID | None = None, by: str = Query(default="revenue", pattern="^(revenue|qty)$"),
              limit: int = Query(default=15, ge=1, le=100), ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> list[dict]:
    start, end = _range(ctx, date_from, date_to)
    lo, hi = svc.day_range(ctx.tenant, start, end)
    return await svc.top_products(db, ctx.tenant, lo, hi, _scope(ctx, branch_id), limit, by)


@router.get("/by-category")
async def category(date_from: date | None = None, date_to: date | None = None, branch_id: uuid.UUID | None = None, ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> list[dict]:
    start, end = _range(ctx, date_from, date_to)
    lo, hi = svc.day_range(ctx.tenant, start, end)
    return await svc.by_category(db, ctx.tenant, lo, hi, _scope(ctx, branch_id))


@router.get("/stock-valuation")
async def valuation(branch_id: uuid.UUID | None = None, ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> dict:
    return await svc.stock_valuation(db, ctx.tenant, _scope(ctx, branch_id), include_cost=ctx.can("view_cost"))


@router.get("/expiring", summary="Lots expiring soon")
async def expiring(days: int = Query(default=90, ge=1, le=720), branch_id: uuid.UUID | None = None,
                   ctx: Ctx = Depends(tenant_ctx(permission=Permission.view_inventory, capability="expiry_tracking", allow_lapsed=True)), db: AsyncSession = Depends(get_db)) -> list[dict]:
    return await svc.expiring(db, ctx.tenant, _scope(ctx, branch_id), days, include_cost=ctx.can("view_cost"))


@router.get("/dues", summary="Who owes what, aged")
async def dues(ctx: Ctx = Depends(tenant_ctx(any_permission=(Permission.collect_dues, Permission.view_reports), capability="customer_credit", allow_lapsed=True)),
               db: AsyncSession = Depends(get_db)) -> dict:
    return await svc.dues(db, ctx.tenant)


@router.get("/exceptions", summary="Sales the system flagged for review")
async def exceptions(date_from: date | None = None, date_to: date | None = None, branch_id: uuid.UUID | None = None,
                     ctx: Ctx = Depends(tenant_ctx(permission=Permission.view_margin, feature="reports", allow_lapsed=True)), db: AsyncSession = Depends(get_db)) -> list[dict]:
    start, end = _range(ctx, date_from or (_today(ctx) - timedelta(days=30)), date_to)
    lo, hi = svc.day_range(ctx.tenant, start, end)
    return await svc.exceptions(db, ctx.tenant, lo, hi, _scope(ctx, branch_id))


@router.get("/cashiers")
async def cashiers(date_from: date | None = None, date_to: date | None = None, branch_id: uuid.UUID | None = None, ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> list[dict]:
    start, end = _range(ctx, date_from, date_to)
    lo, hi = svc.day_range(ctx.tenant, start, end)
    return await svc.cashiers(db, ctx.tenant, lo, hi, _scope(ctx, branch_id))


@router.get("/expenses")
async def expenses(date_from: date | None = None, date_to: date | None = None, branch_id: uuid.UUID | None = None, ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> dict:
    start, end = _range(ctx, date_from, date_to)
    lo, hi = svc.day_range(ctx.tenant, start, end)
    return await svc.expenses_summary(db, ctx.tenant, lo, hi, _scope(ctx, branch_id))


@router.get("/daybook", summary="The day's cash book")
async def daybook(day: date | None = None, branch_id: uuid.UUID | None = None, ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> dict:
    return await svc.daybook(db, ctx.tenant, day or _today(ctx), _scope(ctx, branch_id))


@router.get("/export/sales.csv", summary="Sales as CSV (plan feature: export)")
async def export_sales(date_from: date | None = None, date_to: date | None = None, branch_id: uuid.UUID | None = None,
                       ctx: Ctx = Depends(tenant_ctx(permission=Permission.export_data, feature="export", allow_lapsed=True)),
                       db: AsyncSession = Depends(get_db)) -> Response:
    from sqlalchemy import select
    from app.models.sales import Sale

    start, end = _range(ctx, date_from, date_to)
    lo, hi = svc.day_range(ctx.tenant, start, end)
    scope = _scope(ctx, branch_id)
    conds = [Sale.tenant_id == ctx.tenant.id, Sale.sold_at >= lo, Sale.sold_at < hi]
    if scope:
        conds.append(Sale.branch_id == scope)
    rows = (await db.execute(select(Sale).where(*conds).order_by(Sale.sold_at))).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["invoice_no", "sold_at", "status", "customer", "cashier", "subtotal", "discount", "tax", "rounding", "total", "paid", "due"])
    for s in rows:
        w.writerow([s.invoice_no, s.sold_at.isoformat(), s.status, s.customer_name, s.cashier_name, s.subtotal, s.discount, s.tax_total, s.rounding, s.total, s.paid, s.due])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="sales_{start}_{end}.csv"'})
