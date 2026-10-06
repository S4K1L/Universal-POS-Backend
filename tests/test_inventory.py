import random
from datetime import date, timedelta

from sqlalchemy import text

from tests.conftest import new_id, now_iso
from tests.helpers import create_product, line, pay, push, sale_event


async def lot_product(client, shop, name="Yoghurt"):
    return await create_product(client, shop["headers"], name=name, tracking="lot", track_expiry=True, price="50.00", variant={"cost": "30.00"})


async def receive(client, shop, supplier_id, variant_id, qty, cost, expiry, lot_no, paid="0"):
    r = await client.post("/api/v1/purchases", headers=shop["headers"], json={
        "supplier_id": supplier_id, "paid": paid, "lines": [{"variant_id": variant_id, "qty": qty, "unit_cost": cost, "lot_no": lot_no, "expiry": expiry}]})
    assert r.status_code == 201, r.text
    return r.json()


async def supplier(client, shop):
    r = await client.post("/api/v1/suppliers", headers=shop["headers"], json={"name": "Fresh Dairy Ltd"})
    assert r.status_code == 201
    return r.json()["id"]


async def test_selling_takes_the_earliest_expiry_first_and_records_the_lot(client, make_tenant, db):
    shop = await make_tenant("supershop")
    sid = await supplier(client, shop)
    p = await lot_product(client, shop)
    vid = p["variants"][0]["id"]
    soon, later = (date.today() + timedelta(days=10)).isoformat(), (date.today() + timedelta(days=200)).isoformat()
    await receive(client, shop, sid, vid, "10", "30.00", later, "LATE")
    await receive(client, shop, sid, vid, "10", "32.00", soon, "SOON")  # received second but expires first

    res = await push(client, shop, [sale_event(shop, [line(vid, qty="12", price="50.00")], payments=[pay("600.00")])])
    assert res[0]["status"] == "applied" and res[0]["flags"] == []
    lots = {l["lot_no"]: l["qty"] for l in (await client.get("/api/v1/stock/lots", headers=shop["headers"], params={"variant_id": vid})).json()}
    assert lots == {"LATE": "8.000"}  # SOON (10) emptied, then 2 from LATE

    rows = (await db.execute(text(
        "SELECT l.lot_no, a.qty FROM sale_line_allocations a JOIN stock_lots l ON l.id = a.lot_id ORDER BY l.lot_no"))).all()
    assert [(r[0], str(r[1])) for r in rows] == [("LATE", "2.000"), ("SOON", "10.000")]  # a recall can find every buyer


async def test_expiry_is_required_where_the_product_tracks_it(client, make_tenant):
    shop = await make_tenant("supershop")
    sid = await supplier(client, shop)
    p = await lot_product(client, shop)
    r = await client.post("/api/v1/purchases", headers=shop["headers"], json={
        "supplier_id": sid, "lines": [{"variant_id": p["variants"][0]["id"], "qty": "5", "unit_cost": "30.00"}]})
    assert r.status_code == 422 and r.json()["code"] == "expiry_required"


async def test_expiring_report_lists_what_is_about_to_go_off(client, make_tenant):
    shop = await make_tenant("supershop")
    sid = await supplier(client, shop)
    p = await lot_product(client, shop)
    vid = p["variants"][0]["id"]
    await receive(client, shop, sid, vid, "6", "30.00", (date.today() + timedelta(days=12)).isoformat(), "A1")
    await receive(client, shop, sid, vid, "6", "30.00", (date.today() + timedelta(days=400)).isoformat(), "A2")
    rows = (await client.get("/api/v1/reports/expiring", headers=shop["headers"], params={"days": 30})).json()
    assert [(r["lot_no"], r["days_left"]) for r in rows] == [("A1", 12)] and rows[0]["value_at_cost"] == "180.00"


async def test_cost_is_a_weighted_average_across_receipts(client, make_tenant):
    shop = await make_tenant("local_shop")
    sid = await supplier(client, shop)
    p = await create_product(client, shop["headers"], name="Sugar", price="130.00", variant={"cost": "0.00"})
    vid = p["variants"][0]["id"]
    await receive(client, shop, sid, vid, "10", "100.00", None, "")
    await receive(client, shop, sid, vid, "30", "120.00", None, "")
    got = (await client.get(f"/api/v1/products/{p['id']}", headers=shop["headers"])).json()
    assert got["variants"][0]["cost"] == "115.00"  # (10×100 + 30×120) ÷ 40


async def test_supplier_balance_moves_with_purchases_and_payments(client, make_tenant):
    shop = await make_tenant("local_shop")
    sid = await supplier(client, shop)
    p = await create_product(client, shop["headers"], name="Flour", price="60.00")
    purchase = await receive(client, shop, sid, p["variants"][0]["id"], "20", "50.00", None, "", paid="400.00")
    assert purchase["total"] == "1000.00" and purchase["due"] == "600.00"
    assert (await client.get("/api/v1/suppliers", headers=shop["headers"])).json()[0]["balance"] == "600.00"
    r = await client.post(f"/api/v1/purchases/{purchase['id']}/payments", headers=shop["headers"], json={"amount": "250.00", "method": "bkash"})
    assert r.status_code == 201 and r.json()["due"] == "350.00"
    assert (await client.get("/api/v1/suppliers", headers=shop["headers"])).json()[0]["balance"] == "350.00"
    over = await client.post(f"/api/v1/purchases/{purchase['id']}/payments", headers=shop["headers"], json={"amount": "999.00"})
    assert over.status_code == 409 and over.json()["code"] == "over_payment"


async def test_a_stock_count_posts_the_difference(client, make_tenant):
    shop = await make_tenant("local_shop")
    p = await create_product(client, shop["headers"], name="Soap", price="45.00", variant={"opening_stock": {"qty": "12"}})
    vid = p["variants"][0]["id"]
    res = await push(client, shop, [{"id": new_id(), "type": "stock.adjust", "occurred_at": now_iso(), "payload": {
        "id": new_id(), "variant_id": vid, "branch_id": shop["branch_id"], "counted": "9", "note": "monthly count"}}])
    assert res[0]["status"] == "applied" and res[0]["delta"] == "-3.000"
    mv = (await client.get("/api/v1/stock/movements", headers=shop["headers"], params={"variant_id": vid})).json()
    assert [(m["reason"], m["qty_delta"]) for m in mv][:2] == [("count", "-3.000"), ("opening", "12.000")]


async def test_ledgers_always_agree_with_their_balances(client, make_tenant, db):
    """Randomised: whatever mix of sales, returns, adjustments and collections happens, the books balance."""
    shop = await make_tenant("supershop")
    sid = await supplier(client, shop)
    rng = random.Random(7)
    products = []
    for i in range(4):
        lot = i % 2 == 0
        p = await create_product(client, shop["headers"], name=f"Item {i}", price="25.00", tracking="lot" if lot else "none", track_expiry=lot,
                                 variant={"cost": "15.00"})
        vid = p["variants"][0]["id"]
        if lot:
            for n in range(2):
                await receive(client, shop, sid, vid, "8", "15.00", (date.today() + timedelta(days=30 + n * 40)).isoformat(), f"L{i}{n}")
        else:
            await client.post("/api/v1/stock/adjust", headers=shop["headers"], json={"id": new_id(), "variant_id": vid, "branch_id": shop["branch_id"], "delta": "16", "reason": "opening"})
        products.append(vid)

    cust = new_id()
    await push(client, shop, [{"id": new_id(), "type": "customer.upsert", "occurred_at": now_iso(), "payload": {"id": cust, "name": "Credit Customer"}}])
    sales = []
    for _ in range(40):
        action = rng.choice(["sale", "sale", "sale", "return", "adjust", "collect"])
        if action == "sale":
            vids = rng.sample(products, rng.randint(1, 3))
            lines = [line(v, qty=str(rng.randint(1, 4)), price="25.00") for v in vids]
            total = sum(25 * int(l["qty"]) for l in lines)
            credit = rng.random() < 0.4
            ev = sale_event(shop, lines, payments=[pay(total // 2 if credit else total)], customer_id=cust if credit else None)
            await push(client, shop, [ev])
            sales.append(ev["payload"]["id"])
        elif action == "return" and sales:
            got = (await client.get(f"/api/v1/sales/{rng.choice(sales)}", headers=shop["headers"])).json()
            ln = got["lines"][0]
            left = float(ln["qty"]) - float(ln["returned_qty"])
            if left >= 1:
                await push(client, shop, [{"id": new_id(), "type": "sale.return", "occurred_at": now_iso(), "payload": {
                    "id": new_id(), "sale_id": got["id"], "lines": [{"sale_line_id": ln["id"], "qty": "1"}]}}])
        elif action == "adjust":
            await push(client, shop, [{"id": new_id(), "type": "stock.adjust", "occurred_at": now_iso(), "payload": {
                "id": new_id(), "variant_id": rng.choice(products), "branch_id": shop["branch_id"], "delta": str(rng.choice([-2, -1, 3])), "reason": "adjust"}}])
        elif action == "collect":
            bal = (await client.get(f"/api/v1/customers/{cust}", headers=shop["headers"])).json()["balance"]
            if float(bal) > 0:
                await push(client, shop, [{"id": new_id(), "type": "payment.receive", "occurred_at": now_iso(), "payload": {
                    "id": new_id(), "customer_id": cust, "amount": f"{min(float(bal), 20):.2f}"}}])

    tid = shop["tenant"]["id"]
    reasons = {r[0]: r[1] for r in (await db.execute(text(
        "SELECT reason, count(*) FROM stock_movements WHERE tenant_id = :t GROUP BY reason"), {"t": tid})).all()}
    assert len(sales) > 10 and reasons.get("sale", 0) > 10 and reasons.get("return", 0) >= 1 and reasons.get("adjust", 0) >= 1, reasons
    stock_vs_ledger = (await db.execute(text(
        "SELECT count(*) FROM stock_balances b WHERE b.tenant_id = :t AND b.on_hand <> "
        "COALESCE((SELECT sum(m.qty_delta) FROM stock_movements m WHERE m.branch_id = b.branch_id AND m.variant_id = b.variant_id), 0)"), {"t": tid})).scalar_one()
    lots_vs_ledger = (await db.execute(text(
        "SELECT count(*) FROM stock_lots l WHERE l.tenant_id = :t AND l.qty_on_hand <> "
        "COALESCE((SELECT sum(m.qty_delta) FROM stock_movements m WHERE m.lot_id = l.id), 0)"), {"t": tid})).scalar_one()
    paid_vs_payments = (await db.execute(text(
        "SELECT count(*) FROM sales s WHERE s.tenant_id = :t AND s.paid <> COALESCE((SELECT sum(p.amount) FROM sale_payments p WHERE p.sale_id = s.id), 0)"), {"t": tid})).scalar_one()
    customer_vs_ledger = (await db.execute(text(
        "SELECT count(*) FROM customers c WHERE c.tenant_id = :t AND c.balance <> COALESCE((SELECT sum(l.amount) FROM customer_ledger l WHERE l.customer_id = c.id), 0)"), {"t": tid})).scalar_one()
    negative_lots = (await db.execute(text("SELECT count(*) FROM stock_lots WHERE tenant_id = :t AND qty_on_hand < 0"), {"t": tid})).scalar_one()
    assert (stock_vs_ledger, lots_vs_ledger, paid_vs_payments, customer_vs_ledger, negative_lots) == (0, 0, 0, 0, 0)
