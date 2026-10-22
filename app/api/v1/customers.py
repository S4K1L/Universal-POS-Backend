import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Ctx, tenant_ctx
from app.core.errors import NotFoundError
from app.core.permissions import Permission
from app.db.session import get_db
from app.models.customers import Customer, CustomerLedger
from app.models.sales import Sale
from app.schemas.sales import CustomerUpsertIn, DuePaymentIn
from app.services import sales as sales_service
from app.services import serializers
from app.services.common import utcnow

router = APIRouter(prefix="/customers", tags=["Customers"])
view = tenant_ctx(any_permission=(Permission.manage_customers, Permission.collect_dues, Permission.view_sales), feature="customers", allow_lapsed=True)
write = tenant_ctx(permission=Permission.manage_customers, feature="customers", write=True)


@router.get("", summary="Search customers")
async def list_customers(q: str = "", owing: bool = False, limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                         ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db)) -> dict:
    conds = [Customer.tenant_id == ctx.tenant.id, Customer.active.is_(True)]
    if q.strip():
        like = f"%{q.strip()}%"
        conds.append(or_(Customer.name.ilike(like), Customer.phone.ilike(like)))
    if owing:
        conds.append(Customer.balance > 0)
    total = (await db.execute(select(func.count()).select_from(Customer).where(*conds))).scalar_one()
    rows = (await db.execute(select(Customer).where(*conds).order_by(Customer.name).limit(limit).offset(offset))).scalars().all()
    return {"items": [serializers.customer_out(c) for c in rows], "total": total, "limit": limit, "offset": offset}


@router.post("", status_code=201)
async def upsert_customer(body: CustomerUpsertIn, ctx: Ctx = Depends(write), db: AsyncSession = Depends(get_db)) -> dict:
    c = await db.get(Customer, body.id)
    if c is not None and c.tenant_id != ctx.tenant.id:
        raise NotFoundError("Customer not found")
    if c is None:
        c = Customer(id=body.id, tenant_id=ctx.tenant.id, name=body.name)
        db.add(c)
    c.name, c.phone, c.address, c.group = body.name.strip(), body.phone.strip(), body.address.strip(), body.group
    c.credit_limit, c.notes, c.attributes = body.credit_limit, body.notes, body.attributes
    await db.commit()
    return serializers.customer_out(c)


@router.get("/{customer_id}")
async def get_customer(customer_id: uuid.UUID, ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db)) -> dict:
    c = await db.get(Customer, customer_id)
    if c is None or c.tenant_id != ctx.tenant.id:
        raise NotFoundError("Customer not found")
    return serializers.customer_out(c)


@router.get("/{customer_id}/ledger", summary="Every credit, payment and return on this customer's account")
async def ledger(customer_id: uuid.UUID, limit: int = Query(default=100, ge=1, le=500), ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db)) -> list[dict]:
    c = await db.get(Customer, customer_id)
    if c is None or c.tenant_id != ctx.tenant.id:
        raise NotFoundError("Customer not found")
    rows = (await db.execute(select(CustomerLedger).where(CustomerLedger.customer_id == c.id).order_by(CustomerLedger.at.desc()).limit(limit))).scalars().all()
    return [{"id": str(r.id), "amount": str(r.amount), "kind": r.kind, "note": r.note, "balance_after": str(r.balance_after), "at": r.at.isoformat()} for r in rows]


@router.get("/{customer_id}/open-invoices")
async def open_invoices(customer_id: uuid.UUID, ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Sale).where(Sale.tenant_id == ctx.tenant.id, Sale.customer_id == customer_id, Sale.status == "completed", Sale.total > Sale.paid)
                             .order_by(Sale.sold_at))).scalars().all()
    return [{"sale_id": str(s.id), "invoice_no": s.invoice_no, "sold_at": s.sold_at.isoformat(), "total": str(s.total), "paid": str(s.paid), "due": str(s.due)} for s in rows]


@router.post("/{customer_id}/payments", status_code=201, summary="Collect money against what a customer owes (oldest invoice first)")
async def collect(customer_id: uuid.UUID, body: DuePaymentIn, ctx: Ctx = Depends(tenant_ctx(permission=Permission.collect_dues, feature="customers", write=True)),
                  db: AsyncSession = Depends(get_db)) -> dict:
    body = body.model_copy(update={"customer_id": customer_id})
    touched = await sales_service.receive_due_payment(db, tenant=ctx.tenant, actor=ctx.user, payload=body)
    await db.commit()
    c = await db.get(Customer, customer_id)
    return {"applied_to": [s.invoice_no for s in touched], "balance": str(c.balance)}
