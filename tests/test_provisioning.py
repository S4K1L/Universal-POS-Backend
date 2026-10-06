import pytest

from tests.helpers import create_product, product_body


async def test_business_type_changes_what_the_tenant_is(make_tenant):
    shop = await make_tenant("supershop")
    furniture = await make_tenant("furniture")
    local = await make_tenant("local_shop")

    s, f, l = shop["context"], furniture["context"], local["context"]
    assert "expiry_tracking" in s["capabilities"] and "scale_barcode" in s["capabilities"]
    assert "document_flow" in f["capabilities"] and "expiry_tracking" not in f["capabilities"]
    assert "customer_credit" in l["capabilities"] and "lot_tracking" not in l["capabilities"]

    assert f["settings"]["tax"]["mode"] == "exclusive" and s["settings"]["tax"]["mode"] == "inclusive"
    assert {a["key"] for a in f["attributes"]} >= {"material", "dimensions"}
    assert {a["key"] for a in l["attributes"]} == {"brand", "shelf"}
    assert f["terminology"]["customer"] == "Client" and s["terminology"]["customer"] == "Member"
    assert s["business_type"]["theme"]["accent"] != f["business_type"]["theme"]["accent"]

    mods = lambda ctx: {m["id"]: m for m in ctx["modules"]}
    assert mods(f)["orders"]["available"] and mods(l)["orders"]["locked_reason"] == "capability"
    assert mods(s)["shifts"]["available"] and mods(l)["shifts"]["locked_reason"] == "capability"


async def test_provisioning_copies_the_template_into_the_tenant(client, make_tenant):
    shop = await make_tenant("cosmetic")
    h = shop["headers"]
    cats = (await client.get("/api/v1/categories", headers=h)).json()
    assert {"Skincare", "Makeup"} <= {c["name"] for c in cats}
    tax = (await client.get("/api/v1/tax-classes", headers=h)).json()
    assert any(t["name"] == "VAT 15%" and t["rate"] == "15.00" for t in tax)
    assert shop["context"]["settings"]["language"] == "bn"
    assert [m["code"] for m in shop["context"]["settings"]["payment_methods"] if m["enabled"]][:2] == ["cash", "bkash"]


async def test_phone_is_unique_across_the_platform(client, admin, make_tenant):
    shop = await make_tenant("local_shop")
    r = await client.post("/api/v1/admin/tenants", headers=admin, json={
        "name": "Dup", "owner_name": "Dup Owner", "phone": shop["phone"], "business_type_key": "local_shop", "plan_key": "starter"})
    assert r.status_code == 409 and r.json()["code"] == "phone_taken"


async def test_generated_owner_password_is_returned_once(client, admin):
    r = await client.post("/api/v1/admin/tenants", headers=admin, json={
        "name": "Gen Shop", "owner_name": "Gen Owner", "phone": "01911112222", "business_type_key": "local_shop", "plan_key": "starter"})
    assert r.status_code == 201
    creds = r.json()["owner_login"]
    assert creds["generated"] and len(creds["password"]) >= 8
    ok = await client.post("/api/v1/auth/login", json={"phone": creds["phone"], "password": creds["password"]})
    assert ok.status_code == 200


async def test_attribute_validation_follows_the_business_type(client, make_tenant):
    cosmetic = await make_tenant("cosmetic")
    r = await client.post("/api/v1/products", headers=cosmetic["headers"], json=product_body("Lipstick", attributes={"skin_type": ["Oily"]}))
    assert r.status_code == 422 and r.json()["code"] == "invalid_attributes"  # brand is required for cosmetics
    r = await client.post("/api/v1/products", headers=cosmetic["headers"], json=product_body("Lipstick", attributes={"brand": "Lumé", "skin_type": ["Alien"]}))
    assert r.status_code == 422  # not an option
    r = await client.post("/api/v1/products", headers=cosmetic["headers"], json=product_body("Lipstick", attributes={"brand": "Lumé", "skin_type": ["Oily"], "bogus": 1}))
    assert r.status_code == 201
    assert r.json()["attributes"] == {"brand": "Lumé", "skin_type": ["Oily"]}  # unknown keys dropped


async def test_capability_gates_product_fields(client, make_tenant):
    local = await make_tenant("local_shop")
    r = await client.post("/api/v1/products", headers=local["headers"], json=product_body("Milk", tracking="lot", track_expiry=True))
    assert r.status_code == 403 and r.json()["code"] == "capability_disabled"
    r = await client.post("/api/v1/products", headers=local["headers"], json={
        "name": "Shirt", "variants": [{"price": "10", "option_values": {"size": "L"}}, {"price": "10", "option_values": {"size": "M"}}]})
    assert r.status_code == 403  # variants are not enabled for a local shop
    cosmetic = await make_tenant("cosmetic")
    r = await client.post("/api/v1/products", headers=cosmetic["headers"], json={
        "name": "Lip", "attributes": {"brand": "X"}, "variants": [{"price": "10", "option_values": {"shade": "Rose"}}, {"price": "10", "option_values": {"shade": "Nude"}}]})
    assert r.status_code == 201
    assert [v["label"] for v in r.json()["variants"]] == ["Rose", "Nude"]


async def test_plan_limits_are_enforced_server_side(client, admin, make_tenant):
    r = await client.get("/api/v1/admin/plans", headers=admin)
    starter = next(p for p in r.json()["plans"] if p["key"] == "starter")
    shop = await make_tenant("local_shop", plan="starter")
    assert starter["limits"]["staff"] == 3
    h = shop["headers"]
    for i in range(2):  # owner + 2 = 3
        r = await client.post("/api/v1/users", headers=h, json={"name": f"Staff {i}", "phone": f"0181000000{i}{shop['phone'][-1]}", "password": "staff12345", "role": "cashier"})
        assert r.status_code == 201, r.text
    r = await client.post("/api/v1/users", headers=h, json={"name": "Too many", "phone": "01819999999", "password": "staff12345", "role": "cashier"})
    assert r.status_code == 402 and r.json()["code"] == "plan_limit_exceeded"
    # a plan feature gate: starter has no multi-branch
    b = await client.post("/api/v1/branches", headers=h, json={"name": "Second"})
    assert b.status_code == 403 and b.json()["code"] == "feature_not_in_plan"
