"""Regression tests for the audit findings: delegation on push, pull ordering, session revocation, throttling."""
import asyncio

from sqlalchemy import select, text

from app.models.platform import AuditLog
from tests.conftest import auth, login, new_id, now_iso
from tests.helpers import create_product, line, pay, push, sale_event


async def _staff(client, shop, phone, role, password="staff12345"):
    r = await client.post("/api/v1/users", headers=shop["headers"], json={"name": f"{role} {phone[-3:]}", "phone": phone, "password": password, "role": role})
    assert r.status_code == 201, r.text
    session = await login(client, phone, password)
    return {"id": r.json()["id"], "headers": auth(session["access_token"]), "session": session}


async def _stocked_sale(client, shop):
    p = await create_product(client, shop["headers"], name="Soap", price="100.00", variant={"opening_stock": {"qty": "10", "unit_cost": "60"}})
    return p["variants"][0]["id"]


# ---------------------------------------------------------------------------------- push delegation


async def test_a_cashier_cannot_borrow_the_owners_powers_through_actor_id(client, make_tenant):
    shop = await make_tenant("supershop")
    vid = await _stocked_sale(client, shop)
    owner_id = shop["session"]["user"]["id"]
    cashier = await _staff(client, shop, "01911990001", "cashier")

    sale = sale_event(shop, [line(vid)], payments=[pay("100.00")])
    assert (await push(client, shop, [sale]))[0]["status"] == "applied"
    void = {"id": new_id(), "type": "sale.void", "occurred_at": now_iso(), "actor_id": owner_id,
            "payload": {"sale_id": sale["payload"]["id"], "reason": "borrowed authority"}}
    res = await push(client, shop, [void], headers=cashier["headers"])
    assert res[0]["status"] == "rejected" and res[0]["code"] == "actor_not_permitted"
    sales = (await client.get("/api/v1/sales", headers=shop["headers"])).json()["items"]
    assert sales[0]["status"] == "completed"


async def test_a_till_may_still_hand_over_a_colleagues_queue(client, make_tenant):
    shop = await make_tenant("supershop")
    vid = await _stocked_sale(client, shop)
    a = await _staff(client, shop, "01911990002", "cashier")
    b = await _staff(client, shop, "01911990003", "cashier")
    ev = sale_event(shop, [line(vid)], payments=[pay("100.00")])
    ev["actor_id"] = a["id"]
    assert (await push(client, shop, [ev], headers=b["headers"]))[0]["status"] == "applied"  # cashier B syncs cashier A's sale
    own = sale_event(shop, [line(vid)], payments=[pay("100.00")])
    own["actor_id"] = b["id"]
    assert (await push(client, shop, [own]))[0]["status"] == "applied"  # the owner may submit on behalf of anyone below them


# ---------------------------------------------------------------------------------- pull ordering


async def test_pull_never_moves_the_cursor_past_a_change_that_is_not_committed_yet(client, make_tenant, engine):
    shop = await make_tenant("supershop")
    await create_product(client, shop["headers"], name="Early")
    first = (await client.get("/api/v1/sync/pull", headers=shop["headers"], params={"cursor": 0})).json()
    cursor = first["cursor"]
    tenant_id = shop["tenant"]["id"]

    # Writer A takes the lower seq but is still open; writer B takes the higher seq and commits.
    async with engine.connect() as a, engine.connect() as b:
        ta, tb = await a.begin(), await b.begin()
        ins = text("INSERT INTO sync_changes (tenant_id, entity, entity_id, op) VALUES (:t, 'category', gen_random_uuid(), 'upsert')")
        await a.execute(ins, {"t": tenant_id})
        await b.execute(ins, {"t": tenant_id})
        await tb.commit()

        during = (await client.get("/api/v1/sync/pull", headers=shop["headers"], params={"cursor": cursor})).json()
        assert during["changes"] == [] and during["cursor"] == cursor  # held back: A could still commit below B's seq

        await ta.commit()
    after = (await client.get("/api/v1/sync/pull", headers=shop["headers"], params={"cursor": cursor})).json()
    assert len(after["changes"]) == 2 and after["cursor"] > cursor  # nothing was skipped


# ---------------------------------------------------------------------------------- sessions


async def test_changing_a_password_signs_every_session_out(client, make_tenant):
    shop = await make_tenant("local_shop")
    other_device = (await login(client, shop["phone"], "owner12345"))["refresh_token"]
    r = await client.post("/api/v1/auth/change-password", headers=shop["headers"], json={"current_password": "owner12345", "new_password": "brand-new-1234"})
    assert r.status_code == 204
    for token in (other_device, shop["session"]["refresh_token"]):
        assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": token})).status_code == 401


async def test_resetting_a_staff_or_owner_password_revokes_their_sessions(client, admin, make_tenant):
    shop = await make_tenant("supershop")
    cashier = await _staff(client, shop, "01911990004", "cashier")
    r = await client.patch(f"/api/v1/users/{cashier['id']}", headers=shop["headers"], json={"password": "reset-pass-123"})
    assert r.status_code == 200
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": cashier["session"]["refresh_token"]})).status_code == 401

    reset = await client.post(f"/api/v1/admin/tenants/{shop['tenant']['id']}/reset-owner-password", headers=admin)
    assert reset.status_code == 200
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": shop["session"]["refresh_token"]})).status_code == 401


async def test_a_refresh_retried_straight_away_still_works(client, make_tenant):
    shop = await make_tenant("local_shop")
    old = shop["session"]["refresh_token"]
    first = await client.post("/api/v1/auth/refresh", json={"refresh_token": old})
    again = await client.post("/api/v1/auth/refresh", json={"refresh_token": old})  # the first response never arrived
    assert first.status_code == 200 and again.status_code == 200


async def test_a_stranger_cannot_lock_the_owner_out_from_another_address(client, make_tenant):
    from app.api.v1 import auth as auth_module

    shop = await make_tenant("local_shop")
    # failures recorded for this phone from some other address: the phone-wide ceiling is far higher than the per-address one
    auth_module._FAILS[f"{shop['phone']}|203.0.113.9"] = [__import__("time").time()] * 8
    auth_module._FAILS[shop["phone"]] = [__import__("time").time()] * 8
    ok = await client.post("/api/v1/auth/login", json={"phone": shop["phone"], "password": "owner12345"})
    assert ok.status_code == 200


async def test_repeated_wrong_passwords_from_one_address_are_throttled(client, make_tenant):
    shop = await make_tenant("local_shop")
    for _ in range(8):
        assert (await client.post("/api/v1/auth/login", json={"phone": shop["phone"], "password": "wrong-pass"})).status_code == 401
    assert (await client.post("/api/v1/auth/login", json={"phone": shop["phone"], "password": "owner12345"})).status_code == 429


# ---------------------------------------------------------------------------------- audit + roles


async def test_actions_in_a_support_session_are_tagged_with_the_admin(client, admin, make_tenant, db):
    shop = await make_tenant("supershop")
    imp = auth((await client.post(f"/api/v1/admin/tenants/{shop['tenant']['id']}/impersonate", headers=admin)).json()["access_token"])
    r = await client.post("/api/v1/users", headers=imp, json={"name": "Added By Support", "phone": "01911990005", "password": "staff12345", "role": "cashier"})
    assert r.status_code == 201
    row = (await db.execute(select(AuditLog).where(AuditLog.action == "staff.created", AuditLog.entity_id == r.json()["id"]))).scalar_one()
    assert row.impersonated_by is not None


async def test_the_last_owner_cannot_be_demoted(client, make_tenant):
    shop = await make_tenant("local_shop")
    owner_id = shop["session"]["user"]["id"]
    r = await client.patch(f"/api/v1/users/{owner_id}", headers=shop["headers"], json={"role": "manager"})
    assert r.status_code == 409 and r.json()["code"] == "last_owner"
