import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Ctx, tenant_ctx
from app.core.errors import NotFoundError
from app.core.permissions import Permission
from app.db.session import get_db
from app.models.catalogue import Product, Variant
from app.models.inventory import Purchase, StockBalance, StockLot, StockMovement, Supplier
from app.models.tenant import Branch
from app.schemas.sales import StockAdjustIn
from app.services import purchases as purchase_svc
from app.services import reports as report_svc
from app.services.common import utcnow

router = APIRouter(tags=["Inventory"])
view = tenant_ctx(permission=Permission.view_inventory, feature="inventory", allow_lapsed=True)


async def _branch(db: AsyncSession, ctx: Ctx, branch_id: uuid.UUID | None) -> Branch:
    if branch_id:
        b = await db.get(Branch, branch_id)
        if b is None or b.tenant_id != ctx.tenant.id:
            raise NotFoundError("Branch not found")
        return b
    return (await db.execute(select(Branch).where(Branch.tenant_id == ctx.tenant.id, Branch.is_main.is_(True)))).scalar_one()


@router.get("/stock", summary="What a branch holds, per item")
async def stock(branch_id: uuid.UUID | None = None, q: str = "", low_only: bool = False, limit: int = Query(default=100, ge=1, le=500),
                offset: int = Query(default=0, ge=0), ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db)) -> dict:
    branch = await _branch(db, ctx, branch_id)
    on_hand = func.coalesce(StockBalance.on_hand, 0)
    conds = [Variant.tenant_id == ctx.tenant.id, Product.active.is_(True), Variant.active.is_(True), Product.type == "stockable"]
    if q.strip():
        conds.append(Product.name.ilike(f"%{q.strip()}%") | Variant.sku.ilike(f"%{q.strip()}%"))
    if low_only:
        conds.append(on_hand <= Product.reorder_level)
    base = (select(Variant, Product, on_hand.label("on_hand")).join(Product, Variant.product_id == Product.id)
            .outerjoin(StockBalance, and_(StockBalance.variant_id == Variant.id, StockBalance.branch_id == branch.id)).where(*conds))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(Product.name, Variant.label).limit(limit).offset(offset))).all()
    show_cost = ctx.can("view_cost")
    items = [{
        "variant_id": str(v.id), "product_id": str(p.id), "name": p.name, "label": v.label, "sku": v.sku, "unit": p.base_unit,
        "on_hand": str(oh), "reorder_level": str(p.reorder_level), "price": str(v.price), "cost": str(v.cost) if show_cost else None,
        "tracking": p.tracking, "low": oh <= p.reorder_level,
    } for v, p, oh in rows]
    return {"branch_id": str(branch.id), "items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/stock/movements", summary="The ledger for one item")
async def movements(variant_id: uuid.UUID, branch_id: uuid.UUID | None = None, limit: int = Query(default=50, ge=1, le=200),
                    ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db)) -> list[dict]:
    conds = [StockMovement.tenant_id == ctx.tenant.id, StockMovement.variant_id == variant_id]
    if branch_id:
        conds.append(StockMovement.branch_id == branch_id)
    rows = (await db.execute(select(StockMovement).where(*conds).order_by(StockMovement.occurred_at.desc()).limit(limit))).scalars().all()
    return [{"id": str(m.id), "qty_delta": str(m.qty_delta), "reason": m.reason, "ref_type": m.ref_type, "ref_id": str(m.ref_id) if m.ref_id else None,
             "note": m.note, "occurred_at": m.occurred_at.isoformat(), "unit_cost": str(m.unit_cost) if ctx.can("view_cost") else None} for m in rows]


@router.get("/stock/lots", summary="Batches with expiry")
async def lots(variant_id: uuid.UUID | None = None, branch_id: uuid.UUID | None = None, ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db)) -> list[dict]:
    conds = [StockLot.tenant_id == ctx.tenant.id, StockLot.qty_on_hand > 0]
    if variant_id:
        conds.append(StockLot.variant_id == variant_id)
    if branch_id:
        conds.append(StockLot.branch_id == branch_id)
    rows = (await db.execute(select(StockLot).where(*conds).order_by(StockLot.expiry.asc().nulls_last()).limit(300))).scalars().all()
    return [{"id": str(l.id), "variant_id": str(l.variant_id), "lot_no": l.lot_no, "expiry": l.expiry.isoformat() if l.expiry else None,
             "qty": str(l.qty_on_hand), "branch_id": str(l.branch_id)} for l in rows]


@router.post("/stock/adjust", summary="Correct stock online (the till uses the sync event of the same name)")
async def adjust(body: StockAdjustIn, ctx: Ctx = Depends(tenant_ctx(permission=Permission.adjust_stock, feature="inventory", write=True)),
                 db: AsyncSession = Depends(get_db)) -> dict:
    from app.schemas.sync import SyncEventIn
    from app.services.sync import _h_stock_adjust

    ev = SyncEventIn(id=body.id, type="stock.adjust", occurred_at=utcnow(), payload=body.model_dump(mode="json"))
    result = await _h_stock_adjust(db, ctx.tenant, ctx.user, ev)
    await db.commit()
    return result


# ------------------------------------------------------------------ suppliers & purchases

supplier_view = tenant_ctx(any_permission=(Permission.manage_purchases,), feature="purchases", allow_lapsed=True)
supplier_write = tenant_ctx(permission=Permission.manage_purchases, feature="purchases", write=True)


def supplier_out(s: Supplier) -> dict:
    return {"id": str(s.id), "name": s.name, "company": s.company, "phone": s.phone, "address": s.address, "notes": s.notes,
            "balance": str(s.balance), "active": s.active}


@router.get("/suppliers")
async def list_suppliers(ctx: Ctx = Depends(supplier_view), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Supplier).where(Supplier.tenant_id == ctx.tenant.id).order_by(Supplier.name))).scalars().all()
    return [supplier_out(s) for s in rows]


@router.post("/suppliers", status_code=201)
async def create_supplier(body: purchase_svc.SupplierIn, ctx: Ctx = Depends(supplier_write), db: AsyncSession = Depends(get_db)) -> dict:
    s = Supplier(tenant_id=ctx.tenant.id, **body.model_dump())
    db.add(s)
    await db.commit()
    return supplier_out(s)


@router.patch("/suppliers/{supplier_id}")
async def update_supplier(supplier_id: uuid.UUID, body: purchase_svc.SupplierIn, ctx: Ctx = Depends(supplier_write), db: AsyncSession = Depends(get_db)) -> dict:
    s = await db.get(Supplier, supplier_id)
    if s is None or s.tenant_id != ctx.tenant.id:
        raise NotFoundError("Supplier not found")
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    await db.commit()
    return supplier_out(s)


def purchase_out(p: Purchase) -> dict:
    return {
        "id": str(p.id), "number": p.number, "invoice_no": p.invoice_no, "supplier_id": str(p.supplier_id), "branch_id": str(p.branch_id),
        "purchase_date": p.purchase_date.isoformat(), "status": p.status, "total": str(p.total), "paid": str(p.paid),
        "due": str(max(p.total - p.paid, Decimal("0.00"))), "notes": p.notes,
        "lines": [{"variant_id": str(l.variant_id), "product_name": l.product_name, "qty": str(l.qty), "unit_cost": str(l.unit_cost),
                   "lot_no": l.lot_no, "expiry": l.expiry.isoformat() if l.expiry else None, "line_total": str(l.line_total)} for l in p.lines],
        "payments": [{"amount": str(x.amount), "method": x.method, "paid_at": x.paid_at.isoformat(), "note": x.note} for x in p.payments],
    }


@router.get("/purchases")
async def list_purchases(supplier_id: uuid.UUID | None = None, limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                         ctx: Ctx = Depends(supplier_view), db: AsyncSession = Depends(get_db)) -> dict:
    conds = [Purchase.tenant_id == ctx.tenant.id]
    if supplier_id:
        conds.append(Purchase.supplier_id == supplier_id)
    total = (await db.execute(select(func.count()).select_from(Purchase).where(*conds))).scalar_one()
    rows = (await db.execute(select(Purchase).options(selectinload(Purchase.lines), selectinload(Purchase.payments)).where(*conds)
                             .order_by(Purchase.purchase_date.desc(), Purchase.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [purchase_out(p) for p in rows], "total": total, "limit": limit, "offset": offset}


@router.post("/purchases", status_code=201, summary="Record goods received (and what was paid)")
async def create_purchase(body: purchase_svc.PurchaseIn, ctx: Ctx = Depends(supplier_write), db: AsyncSession = Depends(get_db)) -> dict:
    p = await purchase_svc.create_purchase(db, tenant=ctx.tenant, user=ctx.user, payload=body)
    await db.commit()
    return purchase_out(p)


@router.post("/purchases/{purchase_id}/payments", status_code=201)
async def pay_purchase(purchase_id: uuid.UUID, body: purchase_svc.PurchasePaymentIn, ctx: Ctx = Depends(supplier_write), db: AsyncSession = Depends(get_db)) -> dict:
    p = await purchase_svc.pay_purchase(db, tenant=ctx.tenant, user=ctx.user, purchase_id=purchase_id, payload=body)
    await db.commit()
    return purchase_out(p)
