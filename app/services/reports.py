"""Reporting — every query is date-bounded, tenant-scoped and aggregated in SQL (never "load every sale")."""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Date, and_, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import q2, q3
from app.models.catalogue import Category, Product, Variant
from app.models.customers import Customer
from app.models.inventory import StockBalance, StockLot
from app.models.sales import Expense, Sale, SaleLine, SalePayment, SaleReturn, Shift
from app.models.tenant import Tenant
from app.services.common import tenant_zone, utcnow

ZERO = Decimal("0.00")


def _sum(col, default=0):
    return func.coalesce(func.sum(col), default)


def day_range(tenant: Tenant, start: date, end: date) -> tuple[datetime, datetime]:
    """[start 00:00, end+1 00:00) in the shop's timezone, as UTC."""
    zone = tenant_zone(tenant)
    lo = datetime(start.year, start.month, start.day, tzinfo=zone).astimezone(timezone.utc)
    nxt = end + timedelta(days=1)
    hi = datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone).astimezone(timezone.utc)
    return lo, hi


def _sale_filter(tenant: Tenant, lo: datetime, hi: datetime, branch_id: uuid.UUID | None):
    conds = [Sale.tenant_id == tenant.id, Sale.status == "completed", Sale.sold_at >= lo, Sale.sold_at < hi]
    if branch_id:
        conds.append(Sale.branch_id == branch_id)
    return and_(*conds)


async def summary(session: AsyncSession, tenant: Tenant, lo: datetime, hi: datetime, branch_id: uuid.UUID | None, *, include_margin: bool) -> dict:
    where = _sale_filter(tenant, lo, hi, branch_id)
    row = (await session.execute(
        select(func.count(Sale.id), _sum(Sale.total), _sum(Sale.discount), _sum(Sale.tax_total), _sum(Sale.paid),
               _sum(case((Sale.total > Sale.paid, Sale.total - Sale.paid), else_=0))).where(where)
    )).one()
    count, gross, discount, tax, paid, due = row

    qty = (await session.execute(
        select(_sum(SaleLine.base_qty)).select_from(SaleLine).join(Sale, SaleLine.sale_id == Sale.id).where(where)
    )).scalar_one()

    ret_conds = [SaleReturn.tenant_id == tenant.id, SaleReturn.returned_at >= lo, SaleReturn.returned_at < hi]
    if branch_id:
        ret_conds.append(SaleReturn.branch_id == branch_id)
    ret_count, ret_total = (await session.execute(select(func.count(SaleReturn.id), _sum(SaleReturn.refund_total)).where(*ret_conds))).one()

    voids = (await session.execute(
        select(func.count(Sale.id)).where(Sale.tenant_id == tenant.id, Sale.status == "voided", Sale.sold_at >= lo, Sale.sold_at < hi,
                                          *([Sale.branch_id == branch_id] if branch_id else []))
    )).scalar_one()

    pay_conds = [SalePayment.tenant_id == tenant.id, SalePayment.paid_at >= lo, SalePayment.paid_at < hi, Sale.status == "completed",
                 SalePayment.kind != "return"]
    if branch_id:
        pay_conds.append(Sale.branch_id == branch_id)
    by_method = (await session.execute(
        select(SalePayment.method, _sum(SalePayment.amount), func.count(SalePayment.id))
        .select_from(SalePayment).join(Sale, SalePayment.sale_id == Sale.id).where(*pay_conds)
        .group_by(SalePayment.method).order_by(_sum(SalePayment.amount).desc())
    )).all()

    out = {
        "from": lo.isoformat(), "to": hi.isoformat(),
        "invoices": int(count), "gross_sales": str(q2(gross)), "discounts": str(q2(discount)), "tax": str(q2(tax)),
        "paid": str(q2(paid)), "due_created": str(q2(due)), "items_sold": str(q3(qty)),
        "returns_count": int(ret_count), "returns_total": str(q2(ret_total)), "voids": int(voids),
        "net_sales": str(q2(Decimal(gross) - Decimal(ret_total))),
        "avg_bill": str(q2(Decimal(gross) / count)) if count else "0.00",
        "by_method": [{"method": m, "amount": str(q2(a)), "count": int(c)} for m, a, c in by_method],
    }
    if include_margin:
        revenue_ex_tax, cost = (await session.execute(
            select(
                _sum((SaleLine.charged_total - SaleLine.returned_amount) - SaleLine.tax_amount * (1 - SaleLine.returned_qty / SaleLine.qty)),
                _sum(SaleLine.unit_cost * SaleLine.unit_factor * (SaleLine.qty - SaleLine.returned_qty)),
            ).select_from(SaleLine).join(Sale, SaleLine.sale_id == Sale.id).where(where)
        )).one()
        out["gross_profit"] = str(q2(Decimal(revenue_ex_tax) - Decimal(cost)))
        out["cost_of_goods"] = str(q2(cost))
    return out


async def sales_by_day(session: AsyncSession, tenant: Tenant, start: date, end: date, branch_id: uuid.UUID | None) -> list[dict]:
    lo, hi = day_range(tenant, start, end)
    zone = tenant_zone(tenant).key
    bucket = cast(func.date_trunc("day", func.timezone(zone, Sale.sold_at)), Date)
    rows = (await session.execute(
        select(bucket.label("d"), func.count(Sale.id), _sum(Sale.total)).where(_sale_filter(tenant, lo, hi, branch_id))
        .group_by(bucket).order_by(bucket)
    )).all()
    by_date = {r[0]: (int(r[1]), q2(r[2])) for r in rows}
    out, cursor = [], start
    while cursor <= end:
        count, total = by_date.get(cursor, (0, ZERO))
        out.append({"date": cursor.isoformat(), "invoices": count, "total": str(total)})
        cursor += timedelta(days=1)
    return out


async def sales_by_hour(session: AsyncSession, tenant: Tenant, start: date, end: date, branch_id: uuid.UUID | None) -> list[dict]:
    lo, hi = day_range(tenant, start, end)
    zone = tenant_zone(tenant).key
    hour = func.extract("hour", func.timezone(zone, Sale.sold_at))
    rows = (await session.execute(
        select(hour, func.count(Sale.id), _sum(Sale.total)).where(_sale_filter(tenant, lo, hi, branch_id)).group_by(hour)
    )).all()
    by_hour = {int(r[0]): (int(r[1]), q2(r[2])) for r in rows}
    return [{"hour": h, "invoices": by_hour.get(h, (0, ZERO))[0], "total": str(by_hour.get(h, (0, ZERO))[1])} for h in range(24)]


async def top_products(session: AsyncSession, tenant: Tenant, lo: datetime, hi: datetime, branch_id: uuid.UUID | None, limit: int = 10, by: str = "revenue") -> list[dict]:
    revenue = _sum(SaleLine.charged_total - SaleLine.returned_amount)
    qty = _sum(SaleLine.base_qty - SaleLine.returned_qty * SaleLine.unit_factor)
    rows = (await session.execute(
        select(SaleLine.product_id, SaleLine.product_name, revenue.label("rev"), qty.label("qty"))
        .select_from(SaleLine).join(Sale, SaleLine.sale_id == Sale.id).where(_sale_filter(tenant, lo, hi, branch_id))
        .group_by(SaleLine.product_id, SaleLine.product_name)
        .order_by((qty if by == "qty" else revenue).desc()).limit(limit)
    )).all()
    return [{"product_id": str(r[0]), "name": r[1], "revenue": str(q2(r[2])), "qty": str(q3(r[3]))} for r in rows]


async def by_category(session: AsyncSession, tenant: Tenant, lo: datetime, hi: datetime, branch_id: uuid.UUID | None) -> list[dict]:
    rev = _sum(SaleLine.charged_total - SaleLine.returned_amount)
    rows = (await session.execute(
        select(func.coalesce(Category.name, "Uncategorised"), rev)
        .select_from(SaleLine).join(Sale, SaleLine.sale_id == Sale.id).join(Product, SaleLine.product_id == Product.id)
        .outerjoin(Category, Product.category_id == Category.id).where(_sale_filter(tenant, lo, hi, branch_id))
        .group_by(Category.name).order_by(rev.desc())
    )).all()
    return [{"category": r[0], "revenue": str(q2(r[1]))} for r in rows]


async def stock_valuation(session: AsyncSession, tenant: Tenant, branch_id: uuid.UUID | None, *, include_cost: bool) -> dict:
    conds = [StockBalance.tenant_id == tenant.id, StockBalance.on_hand > 0]
    if branch_id:
        conds.append(StockBalance.branch_id == branch_id)
    cost, retail, units, skus = (await session.execute(
        select(_sum(StockBalance.on_hand * Variant.cost), _sum(StockBalance.on_hand * Variant.price), _sum(StockBalance.on_hand), func.count(StockBalance.id))
        .select_from(StockBalance).join(Variant, StockBalance.variant_id == Variant.id).where(*conds)
    )).one()
    out = {"units": str(q3(units)), "skus": int(skus), "retail_value": str(q2(retail))}
    if include_cost:
        out["cost_value"] = str(q2(cost))
        out["potential_profit"] = str(q2(Decimal(retail) - Decimal(cost)))
    return out


async def low_stock(session: AsyncSession, tenant: Tenant, branch_id: uuid.UUID, limit: int = 50) -> list[dict]:
    on_hand = func.coalesce(StockBalance.on_hand, 0)
    rows = (await session.execute(
        select(Product.name, Variant.id, Variant.label, Variant.sku, on_hand, Product.reorder_level, Product.base_unit)
        .select_from(Variant).join(Product, Variant.product_id == Product.id)
        .outerjoin(StockBalance, and_(StockBalance.variant_id == Variant.id, StockBalance.branch_id == branch_id))
        .where(Variant.tenant_id == tenant.id, Product.active.is_(True), Variant.active.is_(True), Product.type == "stockable", on_hand <= Product.reorder_level)
        .order_by(on_hand.asc(), Product.name).limit(limit)
    )).all()
    return [{"variant_id": str(r[1]), "name": r[0], "label": r[2], "sku": r[3], "on_hand": str(q3(r[4])),
             "reorder_level": str(q3(r[5])), "unit": r[6]} for r in rows]


async def expiring(session: AsyncSession, tenant: Tenant, branch_id: uuid.UUID | None, within_days: int = 90, *, include_cost: bool) -> list[dict]:
    today = date.today()
    conds = [StockLot.tenant_id == tenant.id, StockLot.qty_on_hand > 0, StockLot.expiry.is_not(None), StockLot.expiry <= today + timedelta(days=within_days)]
    if branch_id:
        conds.append(StockLot.branch_id == branch_id)
    rows = (await session.execute(
        select(StockLot, Product.name, Variant.label).join(Variant, StockLot.variant_id == Variant.id)
        .join(Product, Variant.product_id == Product.id).where(*conds).order_by(StockLot.expiry).limit(200)
    )).all()
    out = []
    for lot, name, label in rows:
        item = {"lot_id": str(lot.id), "name": name, "label": label, "lot_no": lot.lot_no, "expiry": lot.expiry.isoformat(),
                "days_left": (lot.expiry - today).days, "qty": str(q3(lot.qty_on_hand))}
        if include_cost:
            item["value_at_cost"] = str(q2(lot.qty_on_hand * lot.unit_cost))
        out.append(item)
    return out


async def dues(session: AsyncSession, tenant: Tenant) -> dict:
    customers = (await session.execute(
        select(Customer).where(Customer.tenant_id == tenant.id, Customer.balance > 0).order_by(Customer.balance.desc()).limit(200)
    )).scalars().all()
    total = (await session.execute(select(_sum(Customer.balance)).where(Customer.tenant_id == tenant.id, Customer.balance > 0))).scalar_one()
    now = utcnow()
    oldest = dict((await session.execute(
        select(Sale.customer_id, func.min(Sale.sold_at)).where(
            Sale.tenant_id == tenant.id, Sale.status == "completed", Sale.total > Sale.paid, Sale.customer_id.is_not(None)
        ).group_by(Sale.customer_id)
    )).all())
    buckets = {"0-30": ZERO, "31-60": ZERO, "61-90": ZERO, "90+": ZERO}
    rows = []
    for c in customers:
        age = (now - oldest[c.id]).days if c.id in oldest else 0
        key = "0-30" if age <= 30 else "31-60" if age <= 60 else "61-90" if age <= 90 else "90+"
        buckets[key] += c.balance
        rows.append({"customer_id": str(c.id), "name": c.name, "phone": c.phone, "balance": str(c.balance), "oldest_days": age})
    return {"total": str(q2(total)), "customers": rows, "ageing": {k: str(q2(v)) for k, v in buckets.items()}}


async def exceptions(session: AsyncSession, tenant: Tenant, lo: datetime, hi: datetime, branch_id: uuid.UUID | None) -> list[dict]:
    conds = [Sale.tenant_id == tenant.id, Sale.sold_at >= lo, Sale.sold_at < hi, func.jsonb_array_length(Sale.flags) > 0]
    if branch_id:
        conds.append(Sale.branch_id == branch_id)
    rows = (await session.execute(select(Sale).where(*conds).order_by(Sale.sold_at.desc()).limit(200))).scalars().all()
    return [{"sale_id": str(s.id), "invoice_no": s.invoice_no, "sold_at": s.sold_at.isoformat(), "cashier": s.cashier_name,
             "total": str(s.total), "flags": s.flags} for s in rows]


async def cashiers(session: AsyncSession, tenant: Tenant, lo: datetime, hi: datetime, branch_id: uuid.UUID | None) -> list[dict]:
    rows = (await session.execute(
        select(Sale.cashier_name, func.count(Sale.id), _sum(Sale.total), _sum(Sale.discount))
        .where(_sale_filter(tenant, lo, hi, branch_id)).group_by(Sale.cashier_name).order_by(_sum(Sale.total).desc())
    )).all()
    return [{"cashier": r[0], "invoices": int(r[1]), "total": str(q2(r[2])), "discounts": str(q2(r[3]))} for r in rows]


async def expenses_summary(session: AsyncSession, tenant: Tenant, lo: datetime, hi: datetime, branch_id: uuid.UUID | None) -> dict:
    conds = [Expense.tenant_id == tenant.id, Expense.spent_at >= lo, Expense.spent_at < hi]
    if branch_id:
        conds.append(Expense.branch_id == branch_id)
    total = (await session.execute(select(_sum(Expense.amount)).where(*conds))).scalar_one()
    rows = (await session.execute(select(Expense.category, _sum(Expense.amount)).where(*conds).group_by(Expense.category).order_by(_sum(Expense.amount).desc()))).all()
    return {"total": str(q2(total)), "by_category": [{"category": r[0], "amount": str(q2(r[1]))} for r in rows]}


async def daybook(session: AsyncSession, tenant: Tenant, day: date, branch_id: uuid.UUID | None) -> dict:
    """The corner-shop cash book: what came in, what went out, what was lent."""
    lo, hi = day_range(tenant, day, day)
    s = await summary(session, tenant, lo, hi, branch_id, include_margin=False)
    exp = await expenses_summary(session, tenant, lo, hi, branch_id)
    pay_conds = [SalePayment.tenant_id == tenant.id, SalePayment.paid_at >= lo, SalePayment.paid_at < hi, SalePayment.kind == "due", Sale.status == "completed"]
    if branch_id:
        pay_conds.append(Sale.branch_id == branch_id)
    collected = (await session.execute(select(_sum(SalePayment.amount)).select_from(SalePayment).join(Sale, SalePayment.sale_id == Sale.id).where(*pay_conds))).scalar_one()
    cash_in = next((Decimal(m["amount"]) for m in s["by_method"] if m["method"] == "cash"), ZERO)
    cash_expense = (await session.execute(select(_sum(Expense.amount)).where(
        Expense.tenant_id == tenant.id, Expense.spent_at >= lo, Expense.spent_at < hi, Expense.method == "cash",
        *([Expense.branch_id == branch_id] if branch_id else [])))).scalar_one()
    return {
        "date": day.isoformat(), "sales": s["gross_sales"], "invoices": s["invoices"], "credit_given": s["due_created"],
        "due_collected": str(q2(collected)), "by_method": s["by_method"], "expenses": exp, "returns": s["returns_total"],
        "cash_in": str(q2(cash_in)), "cash_out": str(q2(cash_expense)), "net_cash": str(q2(cash_in - Decimal(cash_expense))),
    }


async def shift_expected_cash(session: AsyncSession, shift: Shift, until: datetime | None = None) -> Decimal:
    """Float + cash taken − cash paid out, over the shift's window on its own till."""
    until = until or utcnow()
    cash_in = (await session.execute(
        select(_sum(SalePayment.amount)).where(
            SalePayment.tenant_id == shift.tenant_id, SalePayment.method == "cash", SalePayment.kind != "return",
            SalePayment.device_id == shift.device_id, SalePayment.paid_at >= shift.opened_at, SalePayment.paid_at <= until)
    )).scalar_one()
    cash_expense = (await session.execute(
        select(_sum(Expense.amount)).where(
            Expense.tenant_id == shift.tenant_id, Expense.method == "cash", Expense.device_id == shift.device_id,
            Expense.spent_at >= shift.opened_at, Expense.spent_at <= until)
    )).scalar_one()
    cash_refunds = (await session.execute(
        select(_sum(SaleReturn.cash_refunded)).where(
            SaleReturn.tenant_id == shift.tenant_id, SaleReturn.branch_id == shift.branch_id, SaleReturn.refund_method == "cash",
            SaleReturn.returned_at >= shift.opened_at, SaleReturn.returned_at <= until)
    )).scalar_one()
    return q2(shift.opening_float + Decimal(cash_in) - Decimal(cash_expense) - Decimal(cash_refunds))
