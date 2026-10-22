import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Ctx, tenant_ctx
from app.core.errors import NotFoundError
from app.core.permissions import Permission
from app.db.session import get_db
from app.models.sales import Expense, Sale, SaleReturn, Shift
from app.schemas.sales import DuePaymentIn, ExpenseIn, ReturnIn, SaleCreateIn, SaleOut, ShiftCloseIn, ShiftOpenIn, VoidIn
from app.schemas.sync import SyncEventIn
from app.services import reports as report_svc
from app.services import sales as sales_service
from app.services.common import utcnow

router = APIRouter(tags=["Sales"])
view = tenant_ctx(permission=Permission.view_sales, feature="pos", allow_lapsed=True)


def _visible(ctx: Ctx):
    """A branch-restricted account only ever sees sales from its own branches."""
    if ctx.user.branch_ids is None:
        return []
    return [Sale.branch_id.in_([uuid.UUID(b) for b in ctx.user.branch_ids])]


@router.get("/sales", summary="Sales history (paginated)")
async def list_sales(
    q: str = "", branch_id: uuid.UUID | None = None, customer_id: uuid.UUID | None = None, status: str | None = None,
    owing: bool = False, date_from: date | None = None, date_to: date | None = None,
    limit: int = Query(default=30, ge=1, le=200), offset: int = Query(default=0, ge=0),
    ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db),
) -> dict:
    conds = [Sale.tenant_id == ctx.tenant.id, *_visible(ctx)]
    if q.strip():
        like = f"%{q.strip()}%"
        conds.append(or_(Sale.invoice_no.ilike(like), Sale.customer_name.ilike(like)))
    if branch_id:
        conds.append(Sale.branch_id == branch_id)
    if customer_id:
        conds.append(Sale.customer_id == customer_id)
    if status:
        conds.append(Sale.status == status)
    if owing:
        conds.append(Sale.total > Sale.paid)
        conds.append(Sale.status == "completed")
    if date_from or date_to:
        lo, hi = report_svc.day_range(ctx.tenant, date_from or date_to, date_to or date_from)
        conds += [Sale.sold_at >= lo, Sale.sold_at < hi]
    total = (await db.execute(select(func.count()).select_from(Sale).where(*conds))).scalar_one()
    rows = (await db.execute(select(Sale).options(selectinload(Sale.lines), selectinload(Sale.payments)).where(*conds)
                             .order_by(Sale.sold_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [SaleOut.model_validate(s).model_dump(mode="json") for s in rows], "total": total, "limit": limit, "offset": offset}


async def _one(db: AsyncSession, ctx: Ctx, *clauses) -> Sale:
    sale = (await db.execute(select(Sale).options(selectinload(Sale.lines), selectinload(Sale.payments))
                             .where(Sale.tenant_id == ctx.tenant.id, *clauses, *_visible(ctx)))).scalar_one_or_none()
    if sale is None:
        raise NotFoundError("Sale not found")  # 404, not 403: don't confirm another branch's invoice exists
    return sale


@router.get("/sales/by-invoice/{invoice_no}")
async def by_invoice(invoice_no: str, ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db)) -> dict:
    return SaleOut.model_validate(await _one(db, ctx, Sale.invoice_no == invoice_no)).model_dump(mode="json")


@router.get("/sales/{sale_id}")
async def get_sale(sale_id: uuid.UUID, ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db)) -> dict:
    return SaleOut.model_validate(await _one(db, ctx, Sale.id == sale_id)).model_dump(mode="json")


@router.post("/sales", status_code=201, summary="Record a sale online (tills use /sync/push so it works offline too)")
async def create_sale(body: SaleCreateIn, ctx: Ctx = Depends(tenant_ctx(permission=Permission.sell, feature="pos", write=True)), db: AsyncSession = Depends(get_db)) -> dict:
    outcome = await sales_service.create_sale(db, tenant=ctx.tenant, actor=ctx.user, payload=body)
    await db.commit()
    return SaleOut.model_validate(outcome.sale).model_dump(mode="json")


@router.post("/sales/{sale_id}/void")
async def void(sale_id: uuid.UUID, body: VoidIn, ctx: Ctx = Depends(tenant_ctx(permission=Permission.void_sale, feature="pos", write=True)),
               db: AsyncSession = Depends(get_db)) -> dict:
    sale = await sales_service.void_sale(db, tenant=ctx.tenant, actor=ctx.user, payload=body.model_copy(update={"sale_id": sale_id}))
    await db.commit()
    return SaleOut.model_validate(await _one(db, ctx, Sale.id == sale.id)).model_dump(mode="json")


@router.post("/sales/{sale_id}/returns", status_code=201)
async def return_sale(sale_id: uuid.UUID, body: ReturnIn, ctx: Ctx = Depends(tenant_ctx(permission=Permission.refund, feature="pos", write=True)),
                      db: AsyncSession = Depends(get_db)) -> dict:
    ret = await sales_service.create_return(db, tenant=ctx.tenant, actor=ctx.user, payload=body.model_copy(update={"sale_id": sale_id}))
    await db.commit()
    return {"id": str(ret.id), "number": ret.number, "refund_total": str(ret.refund_total), "credit_applied": str(ret.credit_applied), "cash_refunded": str(ret.cash_refunded)}


@router.post("/sales/{sale_id}/payments", status_code=201, summary="Collect a due on one invoice")
async def pay_due(sale_id: uuid.UUID, body: DuePaymentIn, ctx: Ctx = Depends(tenant_ctx(permission=Permission.collect_dues, feature="pos", write=True)),
                  db: AsyncSession = Depends(get_db)) -> dict:
    touched = await sales_service.receive_due_payment(db, tenant=ctx.tenant, actor=ctx.user, payload=body.model_copy(update={"sale_id": sale_id}))
    await db.commit()
    return SaleOut.model_validate(await _one(db, ctx, Sale.id == sale_id)).model_dump(mode="json") if touched else {"duplicate": True}


# ------------------------------------------------------------------ expenses & shifts (read + online write)


@router.get("/expenses")
async def list_expenses(date_from: date | None = None, date_to: date | None = None, limit: int = Query(default=100, ge=1, le=300),
                        ctx: Ctx = Depends(tenant_ctx(permission=Permission.manage_expenses, feature="expenses", allow_lapsed=True)),
                        db: AsyncSession = Depends(get_db)) -> list[dict]:
    conds = [Expense.tenant_id == ctx.tenant.id]
    if date_from or date_to:
        lo, hi = report_svc.day_range(ctx.tenant, date_from or date_to, date_to or date_from)
        conds += [Expense.spent_at >= lo, Expense.spent_at < hi]
    rows = (await db.execute(select(Expense).where(*conds).order_by(Expense.spent_at.desc()).limit(limit))).scalars().all()
    return [{"id": str(e.id), "branch_id": str(e.branch_id), "category": e.category, "amount": str(e.amount), "method": e.method,
             "note": e.note, "spent_at": e.spent_at.isoformat(), "created_by": e.created_by_name} for e in rows]


@router.post("/expenses", status_code=201)
async def add_expense(body: ExpenseIn, ctx: Ctx = Depends(tenant_ctx(permission=Permission.manage_expenses, feature="expenses", write=True)),
                      db: AsyncSession = Depends(get_db)) -> dict:
    from app.services.sync import _h_expense_create

    result = await _h_expense_create(db, ctx.tenant, ctx.user, SyncEventIn(id=uuid.uuid4(), type="expense.create", occurred_at=utcnow(), payload=body.model_dump(mode="json")))
    await db.commit()
    return result


@router.get("/shifts", dependencies=[])
async def list_shifts(limit: int = Query(default=30, ge=1, le=100),
                      ctx: Ctx = Depends(tenant_ctx(permission=Permission.manage_shifts, capability="shifts", allow_lapsed=True)),
                      db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Shift).where(Shift.tenant_id == ctx.tenant.id).order_by(Shift.opened_at.desc()).limit(limit))).scalars().all()
    return [{"id": str(s.id), "branch_id": str(s.branch_id), "opened_by": s.opened_by_name, "opened_at": s.opened_at.isoformat(),
             "closed_at": s.closed_at.isoformat() if s.closed_at else None, "opening_float": str(s.opening_float),
             "counted_cash": str(s.counted_cash) if s.counted_cash is not None else None,
             "expected_cash": str(s.expected_cash) if s.expected_cash is not None else None,
             "variance": str(s.variance) if s.variance is not None else None, "status": s.status, "note": s.note} for s in rows]


@router.get("/shifts/{shift_id}/report", summary="Z-report for a shift")
async def shift_report(shift_id: uuid.UUID, ctx: Ctx = Depends(tenant_ctx(permission=Permission.manage_shifts, capability="shifts", allow_lapsed=True)),
                       db: AsyncSession = Depends(get_db)) -> dict:
    s = await db.get(Shift, shift_id)
    if s is None or s.tenant_id != ctx.tenant.id:
        raise NotFoundError("Shift not found")
    until = s.closed_at or utcnow()
    summary = await report_svc.summary(db, ctx.tenant, s.opened_at, until, s.branch_id, include_margin=False)
    expected = await report_svc.shift_expected_cash(db, s, until)
    return {"shift": {"id": str(s.id), "opened_at": s.opened_at.isoformat(), "closed_at": s.closed_at.isoformat() if s.closed_at else None,
                      "opening_float": str(s.opening_float), "counted_cash": str(s.counted_cash) if s.counted_cash is not None else None,
                      "variance": str(s.variance) if s.variance is not None else None, "status": s.status},
            "expected_cash": str(expected), "summary": summary}
