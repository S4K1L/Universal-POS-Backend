import pytest

from tests.conftest import login, next_phone

BASE = "/api/v1"


def signup_body(phone=None, **kw):
    return {"phone": phone or next_phone(), "password": "secret1234", "business_name": "Rina Cosmetics", "business_type_key": "cosmetic", **kw}


async def test_signup_creates_only_a_pending_request(client, admin, db):
    body = signup_body()
    r = await client.post(f"{BASE}/auth/signup", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "pending" and out["business_name"] == "Rina Cosmetics" and "password" not in str(out).lower().replace("password_hash", "")
    # no tenant, no user yet — signing in says "waiting", not "wrong password"
    r = await client.post(f"{BASE}/auth/login", json={"phone": body["phone"], "password": body["password"]})
    assert r.status_code == 403 and r.json()["code"] == "signup_pending"
    # a wrong password for the same phone is still an ordinary 401 — the status never leaks
    r = await client.post(f"{BASE}/auth/login", json={"phone": body["phone"], "password": "nope-nope-1"})
    assert r.status_code == 401


async def test_status_needs_the_right_password(client):
    body = signup_body()
    await client.post(f"{BASE}/auth/signup", json=body)
    ok = await client.post(f"{BASE}/auth/signup/status", json={"phone": body["phone"], "password": body["password"]})
    assert ok.status_code == 200 and ok.json()["status"] == "pending" and ok.json()["business_type_label"]
    bad = await client.post(f"{BASE}/auth/signup/status", json={"phone": body["phone"], "password": "wrong-pass-1"})
    assert bad.status_code == 404


async def test_validation_and_duplicates(client, make_tenant):
    assert (await client.post(f"{BASE}/auth/signup", json=signup_body(phone="12345678"))).status_code == 422
    assert (await client.post(f"{BASE}/auth/signup", json=signup_body(password="short"))).status_code == 422
    r = await client.post(f"{BASE}/auth/signup", json=signup_body(business_type_key="spaceship"))
    assert r.status_code == 404 and r.json()["code"] == "unknown_business_type"
    shop = await make_tenant("local_shop")
    r = await client.post(f"{BASE}/auth/signup", json=signup_body(phone=shop["phone"]))
    assert r.status_code == 409 and r.json()["code"] == "phone_taken"
    body = signup_body()
    assert (await client.post(f"{BASE}/auth/signup", json=body)).status_code == 201
    r = await client.post(f"{BASE}/auth/signup", json=body)
    assert r.status_code == 409 and r.json()["code"] == "signup_pending"


async def test_only_a_super_admin_sees_the_approvals(client, make_tenant):
    shop = await make_tenant("local_shop")
    assert (await client.get(f"{BASE}/admin/signups", headers=shop["headers"])).status_code == 403
    assert (await client.get(f"{BASE}/admin/signups")).status_code in (401, 403)


async def test_approving_assigns_the_package_and_opens_the_business(client, admin):
    body = signup_body(language="en")
    sid = (await client.post(f"{BASE}/auth/signup", json=body)).json()["id"]
    listed = (await client.get(f"{BASE}/admin/signups", headers=admin)).json()
    assert any(i["id"] == sid for i in listed["items"]) and listed["counts"]["pending"] >= 1
    overview = (await client.get(f"{BASE}/admin/overview", headers=admin)).json()
    assert overview["pending_signups"] >= 1

    r = await client.post(f"{BASE}/admin/signups/{sid}/approve", headers=admin, json={"plan_key": "business", "billing_cycle": "monthly"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["plan_key"] == "business" and r.json()["tenant_id"]

    # the password the applicant chose is the one that works now, and the package is the one the admin picked
    session = await login(client, body["phone"], body["password"])
    ctx = session["context"]
    assert ctx["subscription"]["plan"]["key"] == "business" and ctx["tenant"]["name"] == "Rina Cosmetics"
    assert ctx["business_type"]["key"] == "cosmetic" and ctx["settings"]["language"] == "en"
    # …and it cannot be approved twice
    again = await client.post(f"{BASE}/admin/signups/{sid}/approve", headers=admin, json={"plan_key": "starter"})
    assert again.status_code == 409 and again.json()["code"] == "already_reviewed"
    # the status endpoint now reports approval
    st = await client.post(f"{BASE}/auth/signup/status", json={"phone": body["phone"], "password": body["password"]})
    assert st.json()["status"] == "approved"


async def test_approval_uses_the_plans_trial_and_can_load_samples(client, admin):
    body = signup_body(business_type_key="supershop")
    sid = (await client.post(f"{BASE}/auth/signup", json=body)).json()["id"]
    await client.post(f"{BASE}/admin/signups/{sid}/approve", headers=admin, json={"plan_key": "starter", "trial_days": 30, "sample_data": True})
    session = await login(client, body["phone"], body["password"])
    assert session["context"]["subscription"]["state"] == "trialing"
    products = (await client.get(f"{BASE}/products?limit=5", headers={"Authorization": f"Bearer {session['access_token']}"})).json()
    assert products["total"] > 0


async def test_unknown_plan_is_refused_and_leaves_the_request_pending(client, admin):
    sid = (await client.post(f"{BASE}/auth/signup", json=signup_body())).json()["id"]
    r = await client.post(f"{BASE}/admin/signups/{sid}/approve", headers=admin, json={"plan_key": "platinum"})
    assert r.status_code == 404
    listed = (await client.get(f"{BASE}/admin/signups?status=pending", headers=admin)).json()
    assert any(i["id"] == sid for i in listed["items"])


async def test_rejecting_tells_the_applicant_why_and_allows_a_new_request(client, admin):
    body = signup_body()
    sid = (await client.post(f"{BASE}/auth/signup", json=body)).json()["id"]
    r = await client.post(f"{BASE}/admin/signups/{sid}/reject", headers=admin, json={"reason": "Please add a trade licence number"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    login_try = await client.post(f"{BASE}/auth/login", json={"phone": body["phone"], "password": body["password"]})
    assert login_try.status_code == 403 and login_try.json()["code"] == "signup_rejected" and "trade licence" in login_try.json()["detail"]
    st = await client.post(f"{BASE}/auth/signup/status", json={"phone": body["phone"], "password": body["password"]})
    assert st.json()["status"] == "rejected" and "trade licence" in st.json()["reject_reason"]
    # a fresh request for the same number is allowed once the old one is closed
    assert (await client.post(f"{BASE}/auth/signup", json=body)).status_code == 201
    assert (await client.post(f"{BASE}/admin/signups/{sid}/approve", headers=admin, json={"plan_key": "starter"})).status_code == 409


async def test_signup_is_rate_limited_per_address(client):
    codes = [(await client.post(f"{BASE}/auth/signup", json=signup_body(business_type_key="spaceship"))).status_code for _ in range(12)]
    assert codes.count(429) >= 1
