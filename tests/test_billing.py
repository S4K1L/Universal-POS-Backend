from datetime import datetime, timedelta, timezone

from tests.conftest import auth, new_id, now_iso
from tests.helpers import create_product, line, pay, push, sale_event

PLAN = {"key": "gold", "name": "Gold", "tagline": "test", "monthly_price": "2500.00", "yearly_price": "25000.00", "trial_days": 0,
        "limits": {"products": -1, "staff": -1, "branches": 5, "sales_per_month": -1},
        "features": ["pos", "inventory", "purchases", "customers", "reports", "offline_sync", "staff"], "highlight": False, "public": True, "active": True, "sort_order": 9}


async def channel_id(client, admin):
    return (await client.get("/api/v1/admin/payment-channels", headers=admin)).json()[0]["id"]


async def test_the_super_admin_sets_prices_and_everyone_sees_them(client, admin, make_tenant):
    r = await client.post("/api/v1/admin/plans", headers=admin, json={**PLAN, "key": "gold_a"})
    assert r.status_code == 201
    plan_id = r.json()["id"]

    public = (await client.get("/api/v1/public/plans")).json()["plans"]
    assert next(p for p in public if p["key"] == "gold_a")["monthly_price"] == "2500.00"

    r = await client.put(f"/api/v1/admin/plans/{plan_id}", headers=admin, json={**PLAN, "key": "gold_a", "monthly_price": "2750.50", "yearly_price": "27500.00"})
    assert r.status_code == 200
    shop = await make_tenant("local_shop", plan="gold_a")
    seen = (await client.get("/api/v1/billing/plans", headers=shop["headers"])).json()["plans"]
    assert next(p for p in seen if p["key"] == "gold_a")["monthly_price"] == "2750.50"
    audit = (await client.get("/api/v1/admin/audit", headers=admin, params={"action": "plan.updated"})).json()["items"]
    assert "2500.00→2750.50/mo" in audit[0]["summary"]  # every price change leaves a trail


async def test_a_request_is_priced_on_the_server_and_frozen(client, admin, make_tenant):
    await client.post("/api/v1/admin/plans", headers=admin, json={**PLAN, "key": "gold_b"})
    shop = await make_tenant("local_shop", plan="starter")
    h, ch = shop["headers"], await channel_id(client, admin)
    body = {"plan_key": "gold_b", "billing_cycle": "monthly", "channel_id": ch, "transaction_id": "tx 9a8b7c", "sender_account": "01712345678"}
    r = await client.post("/api/v1/billing/payment-requests", headers=h, json={**body, "amount": "1.00"})
    assert r.status_code == 422  # the client cannot send an amount
    r = await client.post("/api/v1/billing/payment-requests", headers=h, json=body)
    assert r.status_code == 201
    req = r.json()
    assert req["amount"] == "2500.00" and req["transaction_id"] == "TX9A8B7C" and req["status"] == "pending"

    # the admin then raises the price: the pending request keeps the price it was filed at
    plans = (await client.get("/api/v1/admin/plans", headers=admin)).json()["plans"]
    gold = next(p for p in plans if p["key"] == "gold_b")
    await client.put(f"/api/v1/admin/plans/{gold['id']}", headers=admin, json={**PLAN, "key": "gold_b", "monthly_price": "9999.00"})
    mine = (await client.get("/api/v1/billing/payment-requests", headers=h)).json()
    assert mine[0]["amount"] == "2500.00"

    # one pending at a time, and a transaction id funds only one request
    again = await client.post("/api/v1/billing/payment-requests", headers=h, json={**body, "transaction_id": "OTHER-12345"})
    assert again.status_code == 409 and again.json()["code"] == "payment_pending"


async def test_approval_is_the_only_thing_that_moves_the_subscription(client, admin, make_tenant):
    await client.post("/api/v1/admin/plans", headers=admin, json={**PLAN, "key": "gold_c"})
    shop = await make_tenant("local_shop", plan="starter", trial_days=0)
    h, ch = shop["headers"], await channel_id(client, admin)
    before = (await client.get("/api/v1/billing/subscription", headers=h)).json()
    req = (await client.post("/api/v1/billing/payment-requests", headers=h, json={
        "plan_key": "gold_c", "billing_cycle": "yearly", "channel_id": ch, "transaction_id": "BKASH-0001"})).json()
    assert (await client.get("/api/v1/billing/subscription", headers=h)).json()["plan"]["key"] == "starter"  # still unchanged

    queue = (await client.get("/api/v1/admin/payment-requests", headers=admin, params={"status": "pending"})).json()["items"]
    item = next(i for i in queue if i["id"] == req["id"])
    assert item["tenant_name"] == shop["tenant"]["name"] and item["period_end_after"]  # the reviewer sees the exact date first

    r = await client.post(f"/api/v1/admin/payment-requests/{req['id']}/approve", headers=admin, json={"note": "checked statement"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    after = (await client.get("/api/v1/billing/subscription", headers=h)).json()
    assert after["plan"]["key"] == "gold_c" and after["status"] == "active" and after["billing_cycle"] == "yearly"
    assert datetime.fromisoformat(after["period_end"]) > datetime.fromisoformat(before["period_end"]) + timedelta(days=300)
    assert after["invoices"][0]["amount"] == "25000.00" and after["invoices"][0]["reference"] == "BKASH-0001"
    # features follow the plan immediately
    ctx = (await client.get("/api/v1/tenants/me/context", headers=h)).json()
    assert ctx["subscription"]["plan"]["key"] == "gold_c" and "multi_branch" not in ctx["features"]

    twice = await client.post(f"/api/v1/admin/payment-requests/{req['id']}/approve", headers=admin, json={})
    assert twice.status_code == 409 and twice.json()["code"] == "already_reviewed"


async def test_paying_early_stacks_onto_the_current_period(client, admin, make_tenant):
    await client.post("/api/v1/admin/plans", headers=admin, json={**PLAN, "key": "gold_d"})
    shop = await make_tenant("local_shop", plan="gold_d", trial_days=0)
    h, ch = shop["headers"], await channel_id(client, admin)
    end0 = datetime.fromisoformat((await client.get("/api/v1/billing/subscription", headers=h)).json()["period_end"])
    req = (await client.post("/api/v1/billing/payment-requests", headers=h, json={
        "plan_key": "gold_d", "billing_cycle": "monthly", "channel_id": ch, "transaction_id": "RENEW-0001"})).json()
    await client.post(f"/api/v1/admin/payment-requests/{req['id']}/approve", headers=admin, json={})
    end1 = datetime.fromisoformat((await client.get("/api/v1/billing/subscription", headers=h)).json()["period_end"])
    assert end1 - end0 == timedelta(days=30)  # no days lost for renewing early


async def test_rejection_gives_a_reason_and_frees_the_transaction_id(client, admin, make_tenant):
    await client.post("/api/v1/admin/plans", headers=admin, json={**PLAN, "key": "gold_e"})
    shop = await make_tenant("local_shop", plan="starter")
    h, ch = shop["headers"], await channel_id(client, admin)
    body = {"plan_key": "gold_e", "billing_cycle": "monthly", "channel_id": ch, "transaction_id": "WRONG-1234"}
    req = (await client.post("/api/v1/billing/payment-requests", headers=h, json=body)).json()
    r = await client.post(f"/api/v1/admin/payment-requests/{req['id']}/reject", headers=admin, json={"reason": "Amount does not match the statement"})
    assert r.status_code == 200
    mine = (await client.get("/api/v1/billing/payment-requests", headers=h)).json()
    assert mine[0]["status"] == "rejected" and "does not match" in mine[0]["review_note"]
    again = await client.post("/api/v1/billing/payment-requests", headers=h, json=body)
    assert again.status_code == 201  # the rejected id is free to be corrected and refiled


async def test_a_lapsed_account_can_still_read_sync_and_pay_but_not_write(client, admin, make_tenant):
    shop = await make_tenant("local_shop", plan="starter")
    h = shop["headers"]
    p = await create_product(client, h, name="Biscuit", price="20.00", variant={"opening_stock": {"qty": "10"}})
    vid = p["variants"][0]["id"]
    long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    r = await client.patch(f"/api/v1/admin/tenants/{shop['tenant']['id']}", headers=admin, json={"period_end": long_ago})
    assert r.status_code == 200 and r.json()["state"] == "expired"

    ctx = (await client.get("/api/v1/tenants/me/context", headers=h)).json()
    assert ctx["subscription"]["lapsed"] and "renew" in ctx["subscription"]["notice"].lower()
    assert (await client.get("/api/v1/products", headers=h)).status_code == 200  # read
    blocked = await client.post("/api/v1/products", headers=h, json={"name": "New", "variants": [{"price": "5"}]})
    assert blocked.status_code == 402 and blocked.json()["code"] == "subscription_lapsed"
    # a sale already rung up at the till is a fact: it still reaches the books
    res = await push(client, shop, [sale_event(shop, [line(vid, price="20.00")], payments=[pay("20.00")])])
    assert res[0]["status"] == "applied"
    assert (await client.get("/api/v1/billing/payment-channels", headers=h)).status_code == 200  # and billing stays open to recover


async def test_a_suspended_account_is_locked_out_of_everything_but_billing(client, admin, make_tenant):
    shop = await make_tenant("local_shop", plan="starter")
    await client.patch(f"/api/v1/admin/tenants/{shop['tenant']['id']}", headers=admin, json={"status": "suspended", "reason": "fraud review"})
    r = await client.get("/api/v1/products", headers=shop["headers"])
    assert r.status_code == 402 and r.json()["code"] == "account_suspended"
    assert (await client.get("/api/v1/billing/subscription", headers=shop["headers"])).status_code == 200


async def test_grace_days_are_a_platform_setting(client, admin, make_tenant):
    shop = await make_tenant("local_shop", plan="starter")
    two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    r = await client.patch(f"/api/v1/admin/tenants/{shop['tenant']['id']}", headers=admin, json={"period_end": two_days_ago})
    assert r.json()["state"] == "grace"  # default grace is 3 days: still selling
    await client.put("/api/v1/admin/settings/grace_days", headers=admin, json={"days": 1})
    ctx = (await client.get("/api/v1/tenants/me/context", headers=shop["headers"])).json()
    assert ctx["subscription"]["state"] == "expired"
    await client.put("/api/v1/admin/settings/grace_days", headers=admin, json={"days": 3})
