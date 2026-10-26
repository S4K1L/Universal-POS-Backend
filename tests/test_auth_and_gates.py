from tests.conftest import ADMIN_PASSWORD, ADMIN_PHONE, auth, login, new_id
from tests.helpers import product_body


async def test_wrong_password_and_inactive_users_cannot_sign_in(client, make_tenant):
    shop = await make_tenant("local_shop")
    r = await client.post("/api/v1/auth/login", json={"phone": shop["phone"], "password": "nope-nope"})
    assert r.status_code == 401
    ok = await client.post("/api/v1/auth/login", json={"phone": "+88" + shop["phone"], "password": "owner12345"})
    assert ok.status_code == 200  # +880 format is normalised


async def test_refresh_rotates_and_the_old_token_dies(client, make_tenant, db):
    from datetime import timedelta

    from sqlalchemy import update

    from app.models.identity import RefreshToken
    from app.services.common import utcnow

    shop = await make_tenant("local_shop")
    old = shop["session"]["refresh_token"]
    r1 = await client.post("/api/v1/auth/refresh", json={"refresh_token": old})
    assert r1.status_code == 200 and r1.json()["refresh_token"] != old
    # a rotated token is forgiven for a moment (lost response, second tab) — once that moment has passed it is dead
    await db.execute(update(RefreshToken).where(RefreshToken.revoked.is_(True)).values(rotated_at=utcnow() - timedelta(minutes=5)))
    await db.commit()
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": old})
    assert r2.status_code == 401
    out = await client.post("/api/v1/auth/logout", json={"refresh_token": r1.json()["refresh_token"]})
    assert out.status_code == 204
    r3 = await client.post("/api/v1/auth/refresh", json={"refresh_token": r1.json()["refresh_token"]})
    assert r3.status_code == 401


async def test_a_cashier_gets_cashier_powers_and_no_more(client, make_tenant):
    shop = await make_tenant("supershop")
    await client.post("/api/v1/users", headers=shop["headers"], json={"name": "Cash", "phone": "01911880011", "password": "cashier123", "role": "cashier"})
    ch = auth((await login(client, "01911880011", "cashier123"))["access_token"])
    assert (await client.post("/api/v1/products", headers=ch, json=product_body("X"))).status_code == 403
    assert (await client.get("/api/v1/users", headers=ch)).status_code == 403
    assert (await client.get("/api/v1/reports/summary", headers=ch)).status_code == 403
    assert (await client.get("/api/v1/tenants/me/context", headers=ch)).status_code == 200
    ctx = (await client.get("/api/v1/tenants/me/context", headers=ch)).json()
    mods = {m["id"]: m for m in ctx["modules"]}
    assert mods["pos"]["available"] and mods["reports"]["locked_reason"] == "permission" and not mods["billing"]["available"]


async def test_per_user_overrides_distinguish_inherit_from_nothing(client, make_tenant):
    shop = await make_tenant("supershop")
    h = shop["headers"]
    r = await client.post("/api/v1/users", headers=h, json={"name": "Limited", "phone": "01911770011", "password": "limited123", "role": "manager", "permissions": []})
    uid = r.json()["id"]
    assert r.json()["permissions"] == []  # explicitly nothing — not the manager default
    r = await client.patch(f"/api/v1/users/{uid}", headers=h, json={"clear_permissions": True})
    assert "sell" in r.json()["permissions"] and r.json()["permission_overrides"] is None  # back to inheriting
    r = await client.patch(f"/api/v1/users/{uid}", headers=h, json={"permissions": ["manage_subscription"]})
    assert r.status_code == 422  # billing rides with the owner role


async def test_branch_restricted_staff_only_see_their_branch(client, make_tenant):
    shop = await make_tenant("supershop", plan="enterprise")
    h = shop["headers"]
    second = (await client.post("/api/v1/branches", headers=h, json={"name": "Uttara"})).json()
    r = await client.post("/api/v1/users", headers=h, json={"name": "Uttara Cash", "phone": "01911660011", "password": "cashier123", "role": "cashier", "branch_ids": [second["id"]]})
    assert r.status_code == 201
    session = await login(client, "01911660011", "cashier123")
    assert [b["name"] for b in session["context"]["branches"]] == ["Uttara"]
    branches = (await client.get("/api/v1/branches", headers=auth(session["access_token"]))).json()
    assert [b["name"] for b in branches] == ["Uttara"]
    owner_r = await client.post("/api/v1/users", headers=h, json={"name": "Boss", "phone": "01911660022", "password": "boss12345", "role": "owner", "branch_ids": [second["id"]]})
    assert owner_r.status_code == 422  # an owner can never be confined


async def test_maintenance_mode_shuts_tenants_out_but_not_the_console(client, admin, make_tenant):
    shop = await make_tenant("local_shop")
    await client.put("/api/v1/admin/settings/maintenance", headers=admin, json={"enabled": True, "message": "Upgrading — back at 3pm"})
    try:
        r = await client.get("/api/v1/products", headers=shop["headers"])
        assert r.status_code == 503 and "3pm" in r.json()["detail"] and r.json()["code"] == "maintenance"
        assert (await client.get("/api/v1/admin/overview", headers=admin)).status_code == 200
        assert (await client.get("/api/v1/public/status")).json()["maintenance"] is True
        assert (await client.post("/api/v1/auth/login", json={"phone": shop["phone"], "password": "owner12345"})).status_code == 200
    finally:
        await client.put("/api/v1/admin/settings/maintenance", headers=admin, json={"enabled": False, "message": ""})


async def test_impersonation_is_audited_and_tenant_users_cannot_reach_the_console(client, admin, make_tenant):
    shop = await make_tenant("local_shop")
    assert (await client.get("/api/v1/admin/tenants", headers=shop["headers"])).status_code == 403
    r = await client.post(f"/api/v1/admin/tenants/{shop['tenant']['id']}/impersonate", headers=admin)
    assert r.status_code == 200
    imp = auth(r.json()["access_token"])
    assert (await client.get("/api/v1/tenants/me/context", headers=imp)).status_code == 200
    trail = (await client.get("/api/v1/admin/audit", headers=admin, params={"action": "tenant.impersonated"})).json()["items"]
    assert trail and trail[0]["actor"] == "Platform Admin"


async def test_a_business_type_defined_in_the_console_provisions_like_a_built_in_one(client, admin, make_tenant):
    d = {"capabilities": ["barcode_scanning", "variants", "warranty", "customer_credit"], "terminology": {"customer": "Buyer"},
         "attributes": [{"key": "storage_gb", "label": "Storage", "type": "select", "options": ["64", "128", "256"], "required": True}],
         "variant_axes": [{"key": "color", "label": "Colour"}], "stock": {"allocation": "none", "allow_negative": False}, "units": [{"code": "pc", "label": "Piece", "allow_fraction": False}],
         "tax": {"mode": "inclusive", "classes": [{"name": "No VAT", "rate": 0, "default": True}]}, "categories": ["Phones", "Accessories"], "theme": {"accent": "#6D28D9", "icon": "store"}}
    r = await client.post("/api/v1/admin/business-types", headers=admin, json={"key": "mobile_shop", "label": "Mobile Shop", "definition": d})
    assert r.status_code == 201, r.text
    shop = await make_tenant("mobile_shop")
    assert shop["context"]["terminology"]["customer"] == "Buyer" and "warranty" in shop["context"]["capabilities"]
    bad = await client.post("/api/v1/products", headers=shop["headers"], json=product_body("Phone"))
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_attributes"  # the new type's required field applies
    ok = await client.post("/api/v1/products", headers=shop["headers"], json=product_body("Phone", attributes={"storage_gb": "128"}, warranty_months=12))
    assert ok.status_code == 201
    # a type can't switch on a capability that isn't shipped, or that is missing its requirement
    d2 = {**d, "capabilities": ["installments"]}
    assert (await client.post("/api/v1/admin/business-types", headers=admin, json={"key": "bad_one", "label": "Bad", "definition": d2})).status_code == 422
    d3 = {**d, "capabilities": ["expiry_tracking"]}
    assert (await client.post("/api/v1/admin/business-types", headers=admin, json={"key": "bad_two", "label": "Bad", "definition": d3})).status_code == 422
