"""Seed the platform and five demo businesses (one per business type) with two weeks of trading.

    .venv/bin/python -m scripts.seed_demo

Demo logins (password demo12345 for all) are printed at the end.
Platform admin: 01700000000 / admin12345 (development only — set SEED_ADMIN_PASSWORD in production).
"""
import asyncio
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

import app.models  # noqa: E402,F401
from app.core.ids import uuid7  # noqa: E402
from app.core.money import q2  # noqa: E402
from app.core.security import hash_secret  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.catalogue import Product, TaxClass, Variant  # noqa: E402
from app.models.customers import Customer  # noqa: E402
from app.models.identity import User  # noqa: E402
from app.models.inventory import StockBalance, Supplier  # noqa: E402
from app.models.sales import Expense  # noqa: E402
from app.models.tenant import Branch  # noqa: E402
from app.schemas.sales import SaleCreateIn, SaleLineIn, SalePaymentIn  # noqa: E402
from app.schemas.tenant import TenantCreateIn  # noqa: E402
from app.seeds.platform import seed_platform  # noqa: E402
from app.services import orders as order_svc  # noqa: E402
from app.services import purchases as purchase_svc  # noqa: E402
from app.services import sales as sales_service  # noqa: E402
from app.services import subscription  # noqa: E402
from app.services.common import audit, utcnow  # noqa: E402
from app.services.pricing import LineInput, price_cart  # noqa: E402
from app.services.provisioning import provision_tenant  # noqa: E402

PASSWORD = "demo12345"
rng = random.Random(2026)

SHOPS = [
    dict(name="Rahim General Store", owner="Abdur Rahim", phone="01711000001", type="local_shop", plan="starter", city="Mirpur, Dhaka", lang="bn",
         staff=[("Karim Mia", "01711000002", "cashier")], per_day=(16, 30), credit_rate=0.28,
         customers=[("Hasina Begum", "01811100001"), ("Jalal Uddin", "01811100002"), ("Nasima Khatun", "01811100003"), ("Salam Mia", "01811100004"),
                    ("Rina Akter", "01811100005"), ("Babul Hossain", "01811100006")],
         suppliers=["Mirpur Wholesale", "Karwan Bazar Traders"]),
    dict(name="Fresh Bazar Superstore", owner="Tanvir Ahmed", phone="01722000001", type="supershop", plan="business", city="Banani, Dhaka", lang="en",
         staff=[("Mitu Rahman", "01722000002", "manager"), ("Sohel Rana", "01722000003", "cashier"), ("Dina Akter", "01722000004", "cashier")],
         per_day=(26, 46), credit_rate=0.04, second_branch="Uttara",
         customers=[("Farhana Islam", "01822200001"), ("Imran Hossain", "01822200002"), ("Shirin Sultana", "01822200003"), ("Rubel Khan", "01822200004")],
         suppliers=["Agro Fresh Ltd", "City Dairy", "Metro Distributors"]),
    dict(name="Glow & Co Beauty", owner="Nabila Chowdhury", phone="01733000001", type="cosmetic", plan="business", city="Dhanmondi, Dhaka", lang="en",
         staff=[("Priya Das", "01733000002", "cashier")], per_day=(7, 15), credit_rate=0.05,
         customers=[("Tasnim Jahan", "01833300001"), ("Maliha Rahman", "01833300002"), ("Sadia Afrin", "01833300003"), ("Lamia Haque", "01833300004")],
         suppliers=["Seoul Beauty Import", "Lumé Distribution BD"]),
    dict(name="Nakshi Furniture Showroom", owner="Kamal Hossain", phone="01744000001", type="furniture", plan="business", city="Gulshan, Dhaka", lang="en",
         staff=[("Rafiq Ahmed", "01744000002", "salesperson"), ("Sumi Akter", "01744000003", "cashier")], per_day=(1, 3), credit_rate=0.3,
         customers=[("Mrs. Rahman", "01844400001"), ("Dr. Anwar Hossain", "01844400002"), ("Shafiq Enterprise", "01844400003"), ("Mrs. Khatun", "01844400004")],
         suppliers=["Chattogram Wood Works", "Comfort Foam Industries"]),
    dict(name="Shefa Pharmacy", owner="Dr. Mahmud Hasan", phone="01755000001", type="pharmacy", plan="starter", city="Chattogram", lang="en",
         staff=[("Rubina Akter", "01755000002", "cashier")], per_day=(14, 26), credit_rate=0.1,
         customers=[("Abul Kalam", "01855500001"), ("Rokeya Begum", "01855500002"), ("Monir Hossain", "01855500003")],
         suppliers=["Square Distribution", "Beximco Depot"]),
]


def dhaka_to_utc(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo("Asia/Dhaka")).astimezone(timezone.utc)


async def seed_shop(session, admin: User, spec: dict) -> None:
    if (await session.execute(select(User.id).where(User.phone == spec["phone"]))).first():
        print(f"  · {spec['name']} already exists — skipping")
        return
    tenant, owner, _ = await provision_tenant(session, admin=admin, payload=TenantCreateIn(
        name=spec["name"], owner_name=spec["owner"], phone=spec["phone"], password=PASSWORD, business_type_key=spec["type"],
        plan_key=spec["plan"], billing_cycle="monthly", trial_days=0, city=spec["city"], language=spec["lang"], sample_data=True))
    tenant.period_end = utcnow() + timedelta(days=24)
    await session.flush()
    main = (await session.execute(select(Branch).where(Branch.tenant_id == tenant.id, Branch.is_main.is_(True)))).scalar_one()
    main.name = {"supershop": "Banani", "local_shop": "Mirpur 10"}.get(spec["type"], "Main branch")

    users = {"owner": owner}
    for name, phone, role in spec["staff"]:
        u = User(tenant_id=tenant.id, name=name, phone=phone, password_hash=hash_secret(PASSWORD), role=role)
        session.add(u)
        users[role if role not in users else f"{role}2"] = u
    await session.flush()

    branches = [main]
    if spec.get("second_branch"):
        b2 = Branch(tenant_id=tenant.id, name=spec["second_branch"], code=spec["second_branch"][:4].upper(), address="Uttara Sector 7")
        session.add(b2)
        await session.flush()
        branches.append(b2)
        users["cashier2"].branch_ids = [str(b2.id)]  # the second cashier works only at Uttara
    await session.flush()

    customers = []
    for name, phone in spec["customers"]:
        c = Customer(tenant_id=tenant.id, name=name, phone=phone, address="Dhaka", credit_limit=Decimal("20000") if spec["type"] != "supershop" else Decimal("0"))
        session.add(c)
        customers.append(c)
    suppliers = []
    for name in spec["suppliers"]:
        sp = Supplier(tenant_id=tenant.id, name=name, phone="018" + str(rng.randint(10000000, 99999999)))
        session.add(sp)
        suppliers.append(sp)
    await session.flush()

    rows = (await session.execute(select(Variant, Product).join(Product, Variant.product_id == Product.id)
                                  .where(Variant.tenant_id == tenant.id, Product.type == "stockable"))).all()
    taxes = {t.id: t.rate for t in (await session.execute(select(TaxClass).where(TaxClass.tenant_id == tenant.id))).scalars()}
    settings = tenant.settings
    tax_mode, rounding = settings["tax"]["mode"], settings["tax"]["cash_rounding"]
    plan = await subscription.get_plan(session, tenant)
    caps = set(subscription.effective_capabilities(tenant, plan))

    # a second branch needs its own stock: receive a delivery there
    if len(branches) > 1:
        lines = []
        for v, p in rng.sample(rows, min(14, len(rows))):
            lines.append(purchase_svc.PurchaseLineIn(
                variant_id=v.id, qty=Decimal(24), unit_cost=v.cost, lot_no="UTT-1",
                expiry=date.today() + timedelta(days=rng.randint(40, 200)) if p.tracking == "lot" else None))
        await purchase_svc.create_purchase(session, tenant=tenant, user=owner, payload=purchase_svc.PurchaseIn(
            supplier_id=suppliers[0].id, branch_id=branches[1].id, lines=lines, paid=Decimal(0), notes="Opening delivery to Uttara"))

    # restock deliveries, part-paid so there is a supplier balance to see
    for sp in suppliers[:2]:
        lines = []
        for v, p in rng.sample(rows, min(5, len(rows))):
            lines.append(purchase_svc.PurchaseLineIn(
                variant_id=v.id, qty=Decimal(rng.randint(10, 40)), unit_cost=v.cost, lot_no=f"R{rng.randint(100, 999)}",
                expiry=date.today() + timedelta(days=rng.randint(60, 400)) if p.tracking == "lot" else None))
        total = sum(l.qty * l.unit_cost for l in lines)
        await purchase_svc.create_purchase(session, tenant=tenant, user=owner, payload=purchase_svc.PurchaseIn(
            supplier_id=sp.id, branch_id=main.id, invoice_no=f"SUP-{rng.randint(1000, 9999)}",
            purchase_date=date.today() - timedelta(days=rng.randint(2, 9)), lines=lines, paid=q2(total * Decimal("0.6")), pay_method="bkash"))
    await session.flush()

    # what is on the shelf per branch, so history never sells what isn't there
    stock = {(b.branch_id, b.variant_id): b.on_hand for b in (await session.execute(select(StockBalance).where(StockBalance.tenant_id == tenant.id))).scalars()}

    methods = ["cash"] * 6 + ["bkash"] * 2 + ["nagad"] + ["card"]
    sellers = [u for u in users.values() if u.role in ("owner", "manager", "cashier")]
    seq, made = 0, 0
    today, now = date.today(), utcnow()
    for back in range(13, -1, -1):
        day = today - timedelta(days=back)
        lo, hi = spec["per_day"]
        count = rng.randint(lo, hi)
        if day.weekday() in (4, 5):  # Friday and Saturday are the busy days in Bangladesh
            count = int(count * 1.35) + 1
        for _ in range(count):
            when = dhaka_to_utc(day, rng.randint(9, 21), rng.randint(0, 59))
            if when > now:
                continue
            branch = branches[0] if len(branches) == 1 or rng.random() < 0.75 else branches[1]
            actor = rng.choice([u for u in sellers if u.branch_ids is None or str(branch.id) in u.branch_ids] or [owner])
            lines, inputs = [], []
            for v, p in rng.sample(rows, rng.randint(1, min(5, len(rows)))):
                qty = Decimal(rng.choice(["0.5", "1", "1.5", "2", "3"])) if p.allow_fraction else Decimal(rng.randint(1, 3 if spec["type"] != "furniture" else 1))
                if stock.get((branch.id, v.id), Decimal(0)) < qty:
                    continue
                stock[(branch.id, v.id)] -= qty
                rate = taxes.get(p.tax_class_id, Decimal(0)) if "item_tax" in caps and p.tax_class_id else Decimal(0)
                lines.append(SaleLineIn(id=uuid7(), variant_id=v.id, qty=qty, unit=p.base_unit, unit_price=v.price, tax_rate=rate))
                inputs.append(LineInput(v.price, qty, Decimal(0), rate))
            if not lines:
                continue
            priced = price_cart(inputs, tax_mode=tax_mode, cash_rounding=rounding)
            customer = rng.choice(customers) if rng.random() < 0.45 else None
            on_credit = customer is not None and "customer_credit" in caps and rng.random() < spec["credit_rate"] * 2
            paid = priced.total if not on_credit else q2(priced.total * Decimal(rng.choice(["0", "0", "0.3", "0.5"])))
            payments = [SalePaymentIn(id=uuid7(), method=rng.choice(methods), amount=paid, paid_at=when)] if paid > 0 else []
            seq += 1
            await sales_service.create_sale(session, tenant=tenant, actor=actor, payload=SaleCreateIn(
                id=uuid7(), invoice_no=f"INV-D01-{seq:06d}", sold_at=when, branch_id=branch.id, customer_id=customer.id if customer else None,
                customer_name=customer.name if customer else "Walk-in", lines=lines, tax_mode=tax_mode, cash_rounding=rounding, payments=payments))
            made += 1
        await session.flush()

    # the history above drained the shelves, so deliver a fresh stock: most items well stocked, a few left to run low,
    # and a few lots close to expiry so the alerts and the FEFO order have something real to show
    lines = []
    for i, (v, p) in enumerate(rows):
        if i % 7 == 3:
            continue  # left to run low
        qty = Decimal(rng.randint(3, 9)) if spec["type"] == "furniture" else Decimal(rng.randint(18, 60))
        expiry = None
        if p.tracking == "lot":
            expiry = today + timedelta(days=rng.randint(9, 38) if i % 6 == 0 else rng.randint(120, 480))
        lines.append(purchase_svc.PurchaseLineIn(variant_id=v.id, qty=qty, unit_cost=v.cost, lot_no=f"D{rng.randint(1000, 9999)}", expiry=expiry))
    if lines:
        await purchase_svc.create_purchase(session, tenant=tenant, user=owner, payload=purchase_svc.PurchaseIn(
            supplier_id=suppliers[-1].id, branch_id=main.id, invoice_no=f"SUP-{rng.randint(1000, 9999)}", purchase_date=today,
            lines=lines, paid=Decimal(0), notes="Weekly delivery"))
        await session.flush()

    if "expenses" in plan.features:
        for back in range(9, -1, -1):
            for cat, lo, hi in (("Tea & snacks", 60, 220), ("Transport", 150, 600)):
                if rng.random() < 0.6:
                    session.add(Expense(tenant_id=tenant.id, branch_id=main.id, category=cat, amount=Decimal(rng.randint(lo, hi)),
                                        method="cash", note="", spent_at=dhaka_to_utc(today - timedelta(days=back), rng.randint(10, 18), rng.randint(0, 59)),
                                        created_by=owner.id, created_by_name=owner.name))

    if spec["type"] == "furniture":
        await seed_orders(session, tenant, owner, customers, rows)

    await audit(session, action="demo.seeded", actor=admin, tenant_id=None, entity="tenant", entity_id=tenant.id, summary=f"{tenant.name}: {made} sales")
    await session.commit()
    print(f"  ✓ {spec['name']:<28} {spec['type']:<10} {len(rows):>3} items · {made:>3} sales · {len(customers)} customers")


async def seed_orders(session, tenant, owner, customers, rows) -> None:
    by_name: dict[str, list] = {}
    for v, p in rows:
        by_name.setdefault(p.name, []).append(v)
    sofa, bed = by_name["Oslo 3-Seater Sofa"][0], by_name["Haven King Bed"][0]
    table, chair, wardrobe = by_name["Aria Dining Table 6-Seater"][0], by_name["Aria Dining Chair"][0], by_name["Compass Wardrobe 3-Door"][0]

    def line(v, qty, **kw):
        return order_svc.OrderLineIn(variant_id=v.id, qty=Decimal(qty), **kw)

    addr = "Flat 4B, Road 11, Gulshan-1, Dhaka"
    # 1. an open quotation
    await order_svc.create_order(session, tenant=tenant, user=owner, payload=order_svc.OrderIn(
        customer_id=customers[1].id, lines=[line(sofa, 1, spec_note="Charcoal, extra cushions"), line(table, 1, lead_time_days=10), line(chair, 6)],
        discount=Decimal("5000"), delivery_charge=Decimal("2500"), delivery_address=addr, valid_until=date.today() + timedelta(days=10),
        terms="30% advance, balance on delivery. Quotation valid for 10 days."))
    # 2. confirmed with a deposit; delivery in a week
    o2 = await order_svc.create_order(session, tenant=tenant, user=owner, payload=order_svc.OrderIn(
        customer_id=customers[0].id, lines=[line(bed, 1, spec_note="Walnut finish, mattress cut-out", lead_time_days=14), line(wardrobe, 1)],
        delivery_charge=Decimal("3000"), delivery_date=date.today() + timedelta(days=7), delivery_address=addr))
    await order_svc.add_deposit(session, tenant=tenant, user=owner, order_id=o2.id, payload=order_svc.DepositIn(amount=Decimal("60000"), method="bkash", reference="BK7H2QK9"))
    # 3. ready; delivery tomorrow
    o3 = await order_svc.create_order(session, tenant=tenant, user=owner, payload=order_svc.OrderIn(
        customer_id=customers[2].id, lines=[line(chair, 8), line(table, 1)], delivery_date=date.today() + timedelta(days=1), delivery_address="Plot 9, Banani Commercial Area"))
    await order_svc.add_deposit(session, tenant=tenant, user=owner, order_id=o3.id, payload=order_svc.DepositIn(amount=Decimal("50000"), method="cash"))
    await order_svc.set_status(session, tenant=tenant, user=owner, order_id=o3.id, status="ready")
    # 4. delivered and invoiced
    o4 = await order_svc.create_order(session, tenant=tenant, user=owner, payload=order_svc.OrderIn(
        customer_id=customers[3].id, lines=[line(chair, 4)], delivery_charge=Decimal("800"), delivery_address=addr))
    await order_svc.add_deposit(session, tenant=tenant, user=owner, order_id=o4.id, payload=order_svc.DepositIn(amount=Decimal("10000"), method="cash"))
    await order_svc.deliver(session, tenant=tenant, user=owner, order_id=o4.id,
                            payload=order_svc.DeliverIn(payments=[order_svc.DepositIn(amount=o4.total - Decimal("10000"), method="cash")]))


async def main() -> None:
    async with SessionLocal() as session:
        print("Platform:", await seed_platform(session))
        admin = (await session.execute(select(User).where(User.role == "super_admin"))).scalars().first()
        for spec in SHOPS:
            await seed_shop(session, admin, spec)
    print("\nDemo logins (password: demo12345)")
    for spec in SHOPS:
        print(f"  {spec['phone']}  {spec['name']}  [{spec['type']}, {spec['plan']}]  owner")
        for name, phone, role in spec["staff"]:
            print(f"  {phone}  {'':<{len(spec['name'])}}  {role}: {name}")
    print("\nPlatform admin: 01700000000 / admin12345")


if __name__ == "__main__":
    asyncio.run(main())
