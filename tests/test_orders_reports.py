from datetime import date, timedelta

from tests.conftest import new_id, now_iso
from tests.helpers import create_product, line, pay, push, sale_event


async def furniture_setup(client, make_tenant):
    shop = await make_tenant("furniture", plan="business")
    h = shop["headers"]
    sofa = await create_product(client, h, name="Oslo 3-Seater Sofa", price="50000.00", attributes={"material": "Fabric"}, warranty_months=24,
                                variant={"cost": "35000.00", "opening_stock": {"qty": "3", "unit_cost": "35000"}},
                                tax_class_id=next(t["id"] for t in (await client.get("/api/v1/tax-classes", headers=h)).json() if t["name"] == "VAT 15%"))
    cust = (await client.post("/api/v1/customers", headers=h, json={"id": new_id(), "name": "Mrs. Rahman", "phone": "01711000111", "address": "House 5, Road 3, Dhanmondi"})).json()
    return shop, sofa, cust


async def test_quotation_to_delivery_with_a_deposit(client, make_tenant):
    shop, sofa, cust = await furniture_setup(client, make_tenant)
    h = shop["headers"]
    vid = sofa["variants"][0]["id"]
    r = await client.post("/api/v1/orders", headers=h, json={
        "customer_id": cust["id"], "delivery_charge": "1500.00", "lines": [{"variant_id": vid, "qty": "2", "spec_note": "Charcoal, extra cushions", "lead_time_days": 14}]})
    assert r.status_code == 201, r.text
    order = r.json()
    # 2 × 50,000, VAT 15% added on top (this business prices tax-exclusive) + delivery
    assert order["status"] == "quotation" and order["subtotal"] == "100000.00" and order["tax_total"] == "15000.00" and order["total"] == "116500.00"

    # a deposit below the business's minimum (30%) cannot confirm the order
    low = await client.post(f"/api/v1/orders/{order['id']}/deposits", headers=h, json={"amount": "10000.00", "method": "cash"})
    assert low.status_code == 422 and low.json()["code"] == "deposit_too_low"
    ok = await client.post(f"/api/v1/orders/{order['id']}/deposits", headers=h, json={"amount": "40000.00", "method": "bkash", "reference": "DEP1"})
    assert ok.status_code == 201 and ok.json()["status"] == "confirmed" and ok.json()["balance"] == "76500.00"

    ready = await client.post(f"/api/v1/orders/{order['id']}/status", headers=h, json={"status": "ready"})
    assert ready.json()["status"] == "ready"
    r = await client.post(f"/api/v1/orders/{order['id']}/deliver", headers=h, json={"payments": [{"amount": "76500.00", "method": "cash"}]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["order"]["status"] == "delivered" and out["order"]["balance"] == "0.00"
    sale = out["sale"]
    assert sale["total"] == "116500.00" and sale["paid"] == "116500.00" and sale["due"] == "0.00"
    assert {p["method"] for p in sale["payments"]} == {"bkash", "cash"}  # the deposit became part of the invoice
    stock = (await client.get("/api/v1/stock", headers=h)).json()["items"]
    assert next(i for i in stock if i["variant_id"] == vid)["on_hand"] == "1.000"  # stock leaves at delivery, not at quotation
    again = await client.post(f"/api/v1/orders/{order['id']}/deliver", headers=h, json={"payments": []})
    assert again.status_code == 409


async def test_cancelling_a_confirmed_order_records_the_refund(client, make_tenant):
    shop, sofa, cust = await furniture_setup(client, make_tenant)
    h = shop["headers"]
    order = (await client.post("/api/v1/orders", headers=h, json={"customer_id": cust["id"], "lines": [{"variant_id": sofa["variants"][0]["id"], "qty": "1"}]})).json()
    await client.post(f"/api/v1/orders/{order['id']}/deposits", headers=h, json={"amount": "20000.00", "method": "cash"})
    r = await client.post(f"/api/v1/orders/{order['id']}/cancel", headers=h, json={"reason": "changed mind"})
    assert r.json()["status"] == "cancelled"
    assert [(p["kind"], p["amount"]) for p in r.json()["payments"]] == [("deposit", "20000.00"), ("refund", "20000.00")]


async def test_orders_need_the_capability_and_the_plan_feature(client, make_tenant):
    local = await make_tenant("local_shop", plan="business")
    r = await client.get("/api/v1/orders", headers=local["headers"])
    assert r.status_code == 403 and r.json()["code"] == "capability_disabled"
    furn_starter = await make_tenant("furniture", plan="starter")
    r = await client.get("/api/v1/orders", headers=furn_starter["headers"])
    assert r.status_code == 403 and r.json()["code"] == "feature_not_in_plan"


async def test_reports_add_up_to_the_sales(client, make_tenant):
    shop = await make_tenant("supershop")
    h = shop["headers"]
    a = await create_product(client, h, name="A", price="100.00", variant={"cost": "60.00", "opening_stock": {"qty": "100", "unit_cost": "60"}})
    b = await create_product(client, h, name="B", price="40.00", variant={"cost": "25.00", "opening_stock": {"qty": "100", "unit_cost": "25"}})
    va, vb = a["variants"][0]["id"], b["variants"][0]["id"]
    await push(client, shop, [
        sale_event(shop, [line(va, qty="3", price="100.00"), line(vb, qty="2", price="40.00")], payments=[pay("200.00"), pay("180.00", "bkash")]),
        sale_event(shop, [line(va, qty="1", price="100.00")], payments=[pay("100.00", "cash")]),
    ])
    s = (await client.get("/api/v1/reports/summary", headers=h)).json()
    assert s["invoices"] == 2 and s["gross_sales"] == "480.00" and s["items_sold"] == "6.000"
    assert {m["method"]: m["amount"] for m in s["by_method"]} == {"cash": "300.00", "bkash": "180.00"}
    assert s["cost_of_goods"] == "290.00" and s["gross_profit"] == "190.00"  # 480 − (4×60 + 2×25)

    day = (await client.get("/api/v1/reports/dashboard", headers=h)).json()
    assert day["today"]["gross_sales"] == "480.00" and day["trend"][-1]["total"] == "480.00" and len(day["trend"]) == 14
    assert day["top_products"][0]["name"] == "A"
    top = (await client.get("/api/v1/reports/top-products", headers=h, params={"by": "qty"})).json()
    assert [(t["name"], t["qty"]) for t in top] == [("A", "4.000"), ("B", "2.000")]
    val = (await client.get("/api/v1/reports/stock-valuation", headers=h)).json()
    assert val["cost_value"] == "8210.00"  # (96×60) + (98×25) left on the shelf
    book = (await client.get("/api/v1/reports/daybook", headers=h)).json()
    assert book["cash_in"] == "300.00" and book["sales"] == "480.00"


async def test_shift_close_compares_the_count_with_what_the_till_should_hold(client, make_tenant):
    shop = await make_tenant("supershop")
    h = shop["headers"]
    p = await create_product(client, h, name="Water", price="25.00", variant={"opening_stock": {"qty": "50"}})
    vid = p["variants"][0]["id"]
    shift_id = new_id()
    res = await push(client, shop, [
        {"id": new_id(), "type": "shift.open", "occurred_at": now_iso(), "payload": {"id": shift_id, "branch_id": shop["branch_id"], "opened_at": now_iso(), "opening_float": "500.00"}},
        sale_event(shop, [line(vid, qty="4", price="25.00")], payments=[pay("100.00")]),
        sale_event(shop, [line(vid, qty="2", price="25.00")], payments=[pay("50.00", "bkash")]),
        {"id": new_id(), "type": "expense.create", "occurred_at": now_iso(), "payload": {"id": new_id(), "branch_id": shop["branch_id"], "category": "Tea", "amount": "30.00"}},
        {"id": new_id(), "type": "shift.close", "occurred_at": now_iso(), "payload": {"id": shift_id, "closed_at": now_iso(), "counted_cash": "560.00"}},
    ])
    assert [r["status"] for r in res] == ["applied"] * 5
    close = res[-1]
    assert close["expected"] == "570.00"  # 500 float + 100 cash − 30 expense (the bKash sale isn't in the drawer)
    assert close["variance"] == "-10.00"
    z = (await client.get(f"/api/v1/shifts/{shift_id}/report", headers=h)).json()
    assert z["shift"]["status"] == "closed" and z["expected_cash"] == "570.00"


async def test_weighed_items_resolve_from_a_scale_label(client, make_tenant):
    shop = await make_tenant("supershop")
    h = shop["headers"]
    onion = await create_product(client, h, name="Onion", price="65.00", base_unit="kg", allow_fraction=True, variant={"plu": "10001"})
    # EAN-13 scale label: prefix 20 + item code 10001 + weight 01250 g + check digit
    code = "20" + "10001" + "01250" + "7"
    r = await client.get("/api/v1/products/lookup", headers=h, params={"code": code})
    assert r.status_code == 200 and r.json()["variant_id"] == onion["variants"][0]["id"] and r.json()["qty"] == "1.250"
    assert (await client.get("/api/v1/products/lookup", headers=h, params={"code": "9999999999999"})).status_code == 404
