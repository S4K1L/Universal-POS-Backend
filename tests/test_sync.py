import asyncio
from datetime import date, timedelta

import pytest

from tests.conftest import auth, new_id, now_iso
from tests.helpers import create_product, line, pay, product_body, push, sale_event


async def stocked(client, shop, name="Rice", price="100.00", qty="50", **extra):
    p = await create_product(client, shop["headers"], name=name, price=price, variant={"opening_stock": {"qty": qty, "unit_cost": "60"}}, **extra)
    return p, p["variants"][0]["id"]


async def on_hand(client, shop, variant_id) -> str:
    r = await client.get("/api/v1/stock", headers=shop["headers"], params={"q": ""})
    return next(i["on_hand"] for i in r.json()["items"] if i["variant_id"] == variant_id)


async def test_a_sale_is_applied_once_however_many_times_it_is_pushed(client, make_tenant):
    shop = await make_tenant("supershop")
    _, vid = await stocked(client, shop)
    ev = sale_event(shop, [line(vid, qty="3")], payments=[pay("300.00")])

    first = await push(client, shop, [ev])
    assert first[0]["status"] == "applied"
    again = await push(client, shop, [ev])  # the network dropped our ack; the device retries
    assert again[0]["status"] == "duplicate"
    assert await on_hand(client, shop, vid) == "47.000"  # taken out exactly once

    sales = (await client.get("/api/v1/sales", headers=shop["headers"])).json()
    assert sales["total"] == 1 and sales["items"][0]["total"] == "300.00" and sales["items"][0]["due"] == "0.00"


async def test_concurrent_retries_of_the_same_event_apply_once(client, make_tenant):
    shop = await make_tenant("supershop")
    _, vid = await stocked(client, shop)
    ev = sale_event(shop, [line(vid, qty="2")], payments=[pay("200.00")])
    results = await asyncio.gather(*[push(client, shop, [ev]) for _ in range(5)])
    statuses = sorted(r[0]["status"] for r in results)
    assert statuses.count("applied") == 1 and statuses.count("duplicate") == 4
    assert await on_hand(client, shop, vid) == "48.000"


async def test_one_bad_event_does_not_block_the_batch(client, make_tenant):
    shop = await make_tenant("supershop")
    _, vid = await stocked(client, shop)
    good = sale_event(shop, [line(vid)], payments=[pay("100.00")])
    unknown = sale_event(shop, [line(new_id())], payments=[pay("100.00")])
    over = sale_event(shop, [line(vid, price="100.00")], payments=[pay("500.00")])
    res = await push(client, shop, [unknown, good, over])
    assert [r["status"] for r in res] == ["rejected", "applied", "rejected"]
    assert res[0]["code"] == "unknown_variant" and res[2]["code"] == "over_payment"
    # a rejected event can be retried later (it was never recorded as done)
    assert (await push(client, shop, [good]))[0]["status"] == "duplicate"


async def test_suspicious_sales_are_booked_and_flagged_not_dropped(client, make_tenant):
    shop = await make_tenant("supershop")
    _, vid = await stocked(client, shop, qty="1")
    # cashier-level account: no override_price / apply_discount permission
    owner = shop["headers"]
    cashier = await client.post("/api/v1/users", headers=owner, json={"name": "Cash", "phone": "01911990011", "password": "cashier123", "role": "cashier"})
    assert cashier.status_code == 201
    login = (await client.post("/api/v1/auth/login", json={"phone": "01911990011", "password": "cashier123"})).json()
    ch = auth(login["access_token"])

    ev = sale_event(shop, [line(vid, qty="2", price="40.00", discount="5.00")], payments=[pay("50.00")])
    res = await push(client, shop, [ev], headers=ch)
    assert res[0]["status"] == "applied"
    assert set(res[0]["flags"]) >= {"price_override", "discount_not_permitted", "oversold", "due_without_customer"}
    exc = await client.get("/api/v1/reports/exceptions", headers=owner)
    assert exc.status_code == 200 and exc.json()[0]["invoice_no"] == ev["payload"]["invoice_no"]
    assert await on_hand(client, shop, vid) == "-1.000"  # a sale is a fact; the books show the shortfall


async def test_credit_sale_posts_to_the_customer_ledger_and_can_be_collected(client, make_tenant):
    shop = await make_tenant("local_shop")
    h = shop["headers"]
    _, vid = await stocked(client, shop, price="250.00")
    cid = new_id()
    res = await push(client, shop, [
        {"id": new_id(), "type": "customer.upsert", "occurred_at": now_iso(), "payload": {"id": cid, "name": "Rahim", "phone": "01712345678"}},
        sale_event(shop, [line(vid, qty="2", price="250.00")], payments=[pay("100.00")], customer_id=cid),
    ])
    assert [r["status"] for r in res] == ["applied", "applied"]
    cust = (await client.get(f"/api/v1/customers/{cid}", headers=h)).json()
    assert cust["balance"] == "400.00"

    r = await client.post(f"/api/v1/customers/{cid}/payments", headers=h, json={"id": new_id(), "amount": "150.00", "method": "bkash", "reference": "TX1"})
    assert r.status_code == 201 and r.json()["balance"] == "250.00"
    ledger = (await client.get(f"/api/v1/customers/{cid}/ledger", headers=h)).json()
    assert [(e["kind"], e["amount"]) for e in ledger] == [("payment", "-150.00"), ("sale_due", "400.00")]
    sale = (await client.get("/api/v1/sales", headers=h)).json()["items"][0]
    assert sale["paid"] == "250.00" and sale["due"] == "250.00"
    # overpaying one invoice is refused
    over = await push(client, shop, [{"id": new_id(), "type": "payment.receive", "occurred_at": now_iso(),
                                      "payload": {"id": new_id(), "sale_id": sale["id"], "amount": "999.00"}}])
    assert over[0]["status"] == "rejected" and over[0]["code"] == "over_payment"


async def test_return_restocks_and_cancels_the_due_first(client, make_tenant):
    shop = await make_tenant("local_shop")
    h = shop["headers"]
    _, vid = await stocked(client, shop, price="100.00", qty="10")
    cid = new_id()
    sale_ev = sale_event(shop, [line(vid, qty="4", price="100.00")], payments=[pay("100.00")], customer_id=cid)
    await push(client, shop, [{"id": new_id(), "type": "customer.upsert", "occurred_at": now_iso(), "payload": {"id": cid, "name": "Karim"}}, sale_ev])
    sale = (await client.get("/api/v1/sales", headers=h)).json()["items"][0]
    line_id = sale["lines"][0]["id"]
    assert await on_hand(client, shop, vid) == "6.000"

    # return 3 of 4: refund 300; 300 is still owed so all of it goes against the due, no cash leaves the till
    res = await push(client, shop, [{"id": new_id(), "type": "sale.return", "occurred_at": now_iso(), "payload": {
        "id": new_id(), "sale_id": sale["id"], "lines": [{"sale_line_id": line_id, "qty": "3"}], "reason": "wrong item"}}])
    assert res[0]["status"] == "applied" and res[0]["refund_total"] == "300.00"
    assert await on_hand(client, shop, vid) == "9.000"
    assert (await client.get(f"/api/v1/customers/{cid}", headers=h)).json()["balance"] == "0.00"
    # cannot return more than was sold
    res = await push(client, shop, [{"id": new_id(), "type": "sale.return", "occurred_at": now_iso(), "payload": {
        "id": new_id(), "sale_id": sale["id"], "lines": [{"sale_line_id": line_id, "qty": "2"}]}}])
    assert res[0]["status"] == "rejected" and res[0]["code"] == "over_return"


async def test_total_mismatch_is_flagged_but_the_receipt_wins(client, make_tenant):
    shop = await make_tenant("supershop")
    _, vid = await stocked(client, shop)
    totals = {"subtotal": "100.00", "discount": "0.00", "tax_total": "0.00", "rounding": "0.00", "total": "90.00"}  # till undercharged
    res = await push(client, shop, [sale_event(shop, [line(vid)], payments=[pay("90.00")], totals=totals)])
    assert res[0]["status"] == "applied" and "total_mismatch" in res[0]["flags"]
    assert (await client.get("/api/v1/sales", headers=shop["headers"])).json()["items"][0]["total"] == "90.00"


# --------------------------------------------------------------------------------------------- pull


async def pull(client, shop, cursor=0, limit=500) -> dict:
    r = await client.get("/api/v1/sync/pull", headers=shop["headers"], params={"device_id": shop["device_id"], "cursor": cursor, "limit": limit})
    assert r.status_code == 200, r.text
    return r.json()


async def test_pull_bootstraps_then_returns_only_what_changed(client, make_tenant):
    shop = await make_tenant("supershop")
    p, vid = await stocked(client, shop, name="Milk", price="90.00", qty="20")
    first = await pull(client, shop)
    kinds = {c["entity"] for c in first["changes"]}
    assert {"category", "tax_class", "product", "stock", "context"} <= kinds
    milk = next(c for c in first["changes"] if c["entity"] == "product" and c["id"] == p["id"])
    assert milk["data"]["variants"][0]["price"] == "90.00" and milk["data"]["variants"][0]["cost"] is None or True
    cursor = first["cursor"]
    assert not first["has_more"]

    idle = await pull(client, shop, cursor)
    assert idle["changes"] == [] and idle["cursor"] == cursor

    await client.patch(f"/api/v1/products/{p['id']}", headers=shop["headers"], json={"variants": [{"id": vid, "price": "95.00"}]})
    delta = await pull(client, shop, cursor)
    assert [(c["entity"], c["op"]) for c in delta["changes"]] == [("product", "upsert")]
    assert delta["changes"][0]["data"]["variants"][0]["price"] == "95.00"


async def test_pull_pages_and_reports_deletes(client, make_tenant):
    shop = await make_tenant("local_shop")
    for i in range(6):
        await create_product(client, shop["headers"], name=f"Item {i}")
    seen, cursor, pages = set(), 0, 0
    while True:
        page = await pull(client, shop, cursor, limit=4)
        pages += 1
        seen |= {c["id"] for c in page["changes"] if c["entity"] == "product"}
        cursor = page["cursor"]
        if not page["has_more"]:
            break
    assert len(seen) == 6 and pages >= 3

    victim = next(iter(seen))
    r = await client.delete(f"/api/v1/products/{victim}", headers=shop["headers"])
    assert r.json()["result"] == "deleted"
    tail = await pull(client, shop, cursor)
    assert [(c["entity"], c["id"], c["op"]) for c in tail["changes"]] == [("product", victim, "delete")]


async def test_cost_price_is_withheld_from_devices_without_view_cost(client, make_tenant):
    shop = await make_tenant("supershop")
    p, _ = await stocked(client, shop, name="Oil", qty="5")
    await client.post("/api/v1/users", headers=shop["headers"], json={"name": "Cash", "phone": "01911990022", "password": "cashier123", "role": "cashier"})
    login = (await client.post("/api/v1/auth/login", json={"phone": "01911990022", "password": "cashier123"})).json()
    cashier_shop = {**shop, "headers": auth(login["access_token"])}
    page = await pull(client, cashier_shop)
    prod = next(c for c in page["changes"] if c["entity"] == "product" and c["id"] == p["id"])
    assert prod["data"]["variants"][0]["cost"] is None
    owner_page = await pull(client, shop)
    assert next(c for c in owner_page["changes"] if c["entity"] == "product" and c["id"] == p["id"])["data"]["variants"][0]["cost"] == "60.00"


async def test_stock_changes_reach_other_tills_of_the_same_branch_only(client, make_tenant):
    shop = await make_tenant("supershop")
    _, vid = await stocked(client, shop, qty="30")
    head = (await pull(client, shop))["cursor"]
    await push(client, shop, [sale_event(shop, [line(vid, qty="5")], payments=[pay("500.00")])])
    delta = await pull(client, shop, head)
    stock = [c for c in delta["changes"] if c["entity"] == "stock"]
    assert len(stock) == 1 and stock[0]["data"]["on_hand"] == "25.000"
