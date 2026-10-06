import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Ctx, tenant_ctx
from app.core.errors import NotFoundError
from app.core.permissions import Permission
from app.db.session import get_db
from app.models.orders import Order
from app.schemas.common import Strict
from app.schemas.sales import SaleOut
from app.services import orders as svc
from pydantic import Field

router = APIRouter(prefix="/orders", tags=["Orders"])
view = tenant_ctx(permission=Permission.manage_orders, feature="orders", capability="document_flow", allow_lapsed=True)
write = tenant_ctx(permission=Permission.manage_orders, feature="orders", capability="document_flow", write=True)


def order_out(o: Order) -> dict:
    return {
        "id": str(o.id), "number": o.number, "status": o.status, "branch_id": str(o.branch_id), "customer_id": str(o.customer_id),
        "customer_name": o.customer_name, "salesperson_name": o.salesperson_name,
        "valid_until": o.valid_until.isoformat() if o.valid_until else None,
        "delivery_date": o.delivery_date.isoformat() if o.delivery_date else None, "delivery_address": o.delivery_address,
        "delivery_charge": str(o.delivery_charge), "discount": str(o.discount), "subtotal": str(o.subtotal), "tax_total": str(o.tax_total),
        "total": str(o.total), "deposit_paid": str(o.deposit_paid), "balance": str(o.balance), "terms": o.terms, "note": o.note,
        "sale_id": str(o.sale_id) if o.sale_id else None, "created_by": o.created_by_name, "created_at": o.created_at.isoformat(),
        "lines": [{"id": str(l.id), "variant_id": str(l.variant_id), "product_name": l.product_name, "variant_label": l.variant_label,
                   "qty": str(l.qty), "unit_price": str(l.unit_price), "discount": str(l.discount), "tax_rate": str(l.tax_rate),
                   "line_total": str(l.line_total), "spec_note": l.spec_note, "lead_time_days": l.lead_time_days} for l in o.lines],
        "payments": [{"id": str(p.id), "method": p.method, "amount": str(p.amount), "reference": p.reference, "kind": p.kind,
                      "paid_at": p.paid_at.isoformat(), "received_by": p.received_by_name} for p in o.payments],
    }


@router.get("", summary="Quotations and orders")
async def list_orders(status: str | None = None, q: str = "", limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                      ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db)) -> dict:
    conds = [Order.tenant_id == ctx.tenant.id]
    if status:
        conds.append(Order.status == status)
    if q.strip():
        like = f"%{q.strip()}%"
        conds.append(or_(Order.number.ilike(like), Order.customer_name.ilike(like)))
    total = (await db.execute(select(func.count()).select_from(Order).where(*conds))).scalar_one()
    rows = (await db.execute(select(Order).options(selectinload(Order.lines), selectinload(Order.payments)).where(*conds)
                             .order_by(Order.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [order_out(o) for o in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/{order_id}")
async def get_order(order_id: uuid.UUID, ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db)) -> dict:
    return order_out(await svc._load(db, ctx.tenant, order_id))


@router.post("", status_code=201, summary="Draft a quotation")
async def create(body: svc.OrderIn, ctx: Ctx = Depends(write), db: AsyncSession = Depends(get_db)) -> dict:
    o = await svc.create_order(db, tenant=ctx.tenant, user=ctx.user, payload=body)
    await db.commit()
    return order_out(o)


@router.put("/{order_id}", summary="Re-quote (only while still a quotation)")
async def replace(order_id: uuid.UUID, body: svc.OrderIn, ctx: Ctx = Depends(write), db: AsyncSession = Depends(get_db)) -> dict:
    o = await svc.replace_order(db, tenant=ctx.tenant, user=ctx.user, order_id=order_id, payload=body)
    await db.commit()
    return order_out(o)


@router.post("/{order_id}/deposits", status_code=201, summary="Take a deposit; the first one that meets the minimum confirms the order")
async def deposit(order_id: uuid.UUID, body: svc.DepositIn, ctx: Ctx = Depends(write), db: AsyncSession = Depends(get_db)) -> dict:
    o = await svc.add_deposit(db, tenant=ctx.tenant, user=ctx.user, order_id=order_id, payload=body)
    await db.commit()
    return order_out(o)


class StatusIn(Strict):
    status: str = Field(pattern="^(ready|confirmed)$")


@router.post("/{order_id}/status")
async def status_(order_id: uuid.UUID, body: StatusIn, ctx: Ctx = Depends(write), db: AsyncSession = Depends(get_db)) -> dict:
    o = await svc.set_status(db, tenant=ctx.tenant, user=ctx.user, order_id=order_id, status=body.status)
    await db.commit()
    return order_out(o)


class CancelIn(Strict):
    reason: str = Field(default="", max_length=300)


@router.post("/{order_id}/cancel")
async def cancel(order_id: uuid.UUID, body: CancelIn, ctx: Ctx = Depends(write), db: AsyncSession = Depends(get_db)) -> dict:
    o = await svc.cancel_order(db, tenant=ctx.tenant, user=ctx.user, order_id=order_id, reason=body.reason)
    await db.commit()
    return order_out(o)


@router.post("/{order_id}/deliver", summary="Deliver: raises the invoice, takes the stock, applies deposits")
async def deliver(order_id: uuid.UUID, body: svc.DeliverIn, ctx: Ctx = Depends(write), db: AsyncSession = Depends(get_db)) -> dict:
    order, sale = await svc.deliver(db, tenant=ctx.tenant, user=ctx.user, order_id=order_id, payload=body)
    await db.commit()
    from sqlalchemy.orm import selectinload as sl
    from app.models.sales import Sale

    sale = (await db.execute(select(Sale).options(sl(Sale.lines), sl(Sale.payments)).where(Sale.id == sale.id))).scalar_one()
    return {"order": order_out(order), "sale": SaleOut.model_validate(sale).model_dump(mode="json")}
