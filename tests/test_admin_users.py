from tests.conftest import ADMIN_PASSWORD, ADMIN_PHONE, auth, login, next_phone

BASE = "/api/v1"


async def test_user_list_covers_every_account_on_the_platform(client, admin, make_tenant):
    shop = await make_tenant("local_shop")
    staff = await client.post(f"{BASE}/users", headers=shop["headers"], json={"name": "Cash Ier", "phone": next_phone(), "password": "cashier1234", "role": "cashier"})
    assert staff.status_code == 201, staff.text

    r = await client.get(f"{BASE}/admin/users?limit=200", headers=admin)
    assert r.status_code == 200
    body = r.json()
    by_phone = {u["phone"]: u for u in body["items"]}
    assert by_phone[ADMIN_PHONE]["role"] == "super_admin" and by_phone[ADMIN_PHONE]["tenant_name"] is None
    owner = by_phone[shop["phone"]]
    assert owner["role"] == "owner" and owner["tenant_name"] == shop["tenant"]["name"]
    assert staff.json()["phone"] in by_phone
    assert "password_hash" not in str(body)
    assert body["counts"]["all"] == body["total"] and body["counts"]["admin"] >= 1 and body["counts"]["owner"] >= 1 and body["counts"]["staff"] >= 1


async def test_user_list_filters_and_search(client, admin, make_tenant):
    shop = await make_tenant("local_shop")
    only_admins = (await client.get(f"{BASE}/admin/users?group=admin", headers=admin)).json()
    assert only_admins["items"] and {u["role"] for u in only_admins["items"]} == {"super_admin"}
    owners = (await client.get(f"{BASE}/admin/users?group=owner&limit=200", headers=admin)).json()
    assert shop["phone"] in {u["phone"] for u in owners["items"]}
    by_business = (await client.get(f"{BASE}/admin/users", params={"q": shop["tenant"]["name"]}, headers=admin)).json()
    assert [u["phone"] for u in by_business["items"]] == [shop["phone"]]
    by_phone = (await client.get(f"{BASE}/admin/users", params={"q": shop["phone"][-6:]}, headers=admin)).json()
    assert shop["phone"] in {u["phone"] for u in by_phone["items"]}
    page = (await client.get(f"{BASE}/admin/users?limit=2&offset=0", headers=admin)).json()
    assert len(page["items"]) == 2 and page["total"] > 2


async def test_only_a_super_admin_can_see_users_or_manage_admins(client, make_tenant):
    shop = await make_tenant("local_shop")
    for method, path in (("get", "/admin/users"), ("get", "/admin/admins")):
        assert (await getattr(client, method)(f"{BASE}{path}", headers=shop["headers"])).status_code == 403
    r = await client.post(f"{BASE}/admin/admins", headers=shop["headers"], json={"name": "Sneaky", "phone": next_phone(), "password": "password123"})
    assert r.status_code == 403


async def test_a_new_admin_can_sign_in_and_use_the_console(client, admin):
    phone = next_phone()
    r = await client.post(f"{BASE}/admin/admins", headers=admin, json={"name": "Second Admin", "phone": phone, "password": "second12345"})
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "super_admin" and r.json()["active"] is True
    session = await login(client, phone, "second12345")
    assert session["user"]["role"] == "super_admin" and session["context"] is None
    assert (await client.get(f"{BASE}/admin/overview", headers=auth(session["access_token"]))).status_code == 200
    listed = (await client.get(f"{BASE}/admin/admins", headers=admin)).json()
    assert phone in {a["phone"] for a in listed}


async def test_adding_validates_and_refuses_taken_numbers(client, admin, make_tenant):
    shop = await make_tenant("local_shop")
    assert (await client.post(f"{BASE}/admin/admins", headers=admin, json={"name": "X Y", "phone": "12345678", "password": "password123"})).status_code == 422
    assert (await client.post(f"{BASE}/admin/admins", headers=admin, json={"name": "X Y", "phone": next_phone(), "password": "short"})).status_code == 422
    r = await client.post(f"{BASE}/admin/admins", headers=admin, json={"name": "Dup", "phone": shop["phone"], "password": "password123"})
    assert r.status_code == 409 and r.json()["code"] == "phone_taken"
    again = await client.post(f"{BASE}/admin/admins", headers=admin, json={"name": "Dup", "phone": ADMIN_PHONE, "password": "password123"})
    assert again.status_code == 409


async def test_removing_an_admin_signs_them_out_at_once_and_keeps_the_record(client, admin):
    phone = next_phone()
    created = (await client.post(f"{BASE}/admin/admins", headers=admin, json={"name": "Temp Admin", "phone": phone, "password": "temp123456"})).json()
    session = await login(client, phone, "temp123456")
    token = auth(session["access_token"])
    assert (await client.get(f"{BASE}/admin/overview", headers=token)).status_code == 200

    r = await client.delete(f"{BASE}/admin/admins/{created['id']}", headers=admin)
    assert r.status_code == 200 and r.json()["active"] is False
    # the token they already hold stops working, and they cannot sign in again
    assert (await client.get(f"{BASE}/admin/overview", headers=token)).status_code == 401
    assert (await client.post(f"{BASE}/auth/login", json={"phone": phone, "password": "temp123456"})).status_code == 401
    refresh = await client.post(f"{BASE}/auth/refresh", json={"refresh_token": session["refresh_token"]})
    assert refresh.status_code == 401
    # still listed (as removed) so history stays readable
    listed = {a["phone"]: a for a in (await client.get(f"{BASE}/admin/admins", headers=admin)).json()}
    assert listed[phone]["active"] is False
    audit = (await client.get(f"{BASE}/admin/audit?limit=20", headers=admin)).json()
    assert any(a["action"] == "admin.removed" for a in audit["items"])


async def test_a_removed_admin_can_be_added_back(client, admin):
    phone = next_phone()
    first = (await client.post(f"{BASE}/admin/admins", headers=admin, json={"name": "Comes Back", "phone": phone, "password": "first12345"})).json()
    await client.delete(f"{BASE}/admin/admins/{first['id']}", headers=admin)
    r = await client.post(f"{BASE}/admin/admins", headers=admin, json={"name": "Comes Back Again", "phone": phone, "password": "second12345"})
    assert r.status_code == 201 and r.json()["id"] == first["id"] and r.json()["active"] is True
    assert (await client.post(f"{BASE}/auth/login", json={"phone": phone, "password": "first12345"})).status_code == 401
    assert (await login(client, phone, "second12345"))["user"]["name"] == "Comes Back Again"


async def test_you_cannot_remove_yourself_or_the_last_admin(client, admin):
    me = (await login(client, ADMIN_PHONE, ADMIN_PASSWORD))["user"]
    r = await client.delete(f"{BASE}/admin/admins/{me['id']}", headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "cannot_remove_self"

    # with another admin present, that admin cannot be left as the only one by removing the other either way
    phone = next_phone()
    other = (await client.post(f"{BASE}/admin/admins", headers=admin, json={"name": "Other", "phone": phone, "password": "other123456"})).json()
    other_headers = auth((await login(client, phone, "other123456"))["access_token"])
    # Other removes the seeded admin → allowed while Other remains …
    others_view = await client.delete(f"{BASE}/admin/admins/{me['id']}", headers=other_headers)
    assert others_view.status_code == 200
    # … and now Other is the last one standing: nobody can remove them (and they cannot remove themselves)
    assert (await client.delete(f"{BASE}/admin/admins/{other['id']}", headers=other_headers)).status_code == 409
    # put the seeded admin back so other tests still have one
    await client.post(f"{BASE}/admin/admins", headers=other_headers, json={"name": "Platform Admin", "phone": ADMIN_PHONE, "password": ADMIN_PASSWORD})
    await client.delete(f"{BASE}/admin/admins/{other['id']}", headers=auth((await login(client, ADMIN_PHONE, ADMIN_PASSWORD))["access_token"]))


async def test_removing_a_non_admin_or_unknown_id_is_a_404(client, admin, make_tenant):
    shop = await make_tenant("local_shop")
    owner_id = shop["session"]["user"]["id"]
    assert (await client.delete(f"{BASE}/admin/admins/{owner_id}", headers=admin)).status_code == 404
    assert (await client.delete(f"{BASE}/admin/admins/00000000-0000-0000-0000-000000000000", headers=admin)).status_code == 404
