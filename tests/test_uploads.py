import io

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
BASE = "/api/v1"


async def upload(client, h, data, name="x.png", ctype="image/png"):
    return await client.post(f"{BASE}/uploads/images", headers=h, files={"file": (name, io.BytesIO(data), ctype)})


async def test_a_product_picture_is_stored_and_served(client, make_tenant):
    shop = await make_tenant("supershop")
    r = await upload(client, shop["headers"], PNG)
    assert r.status_code == 201, r.text
    url = r.json()["url"]
    assert url.startswith(f"/uploads/{shop['tenant']['id']}/") and url.endswith(".png")
    served = await client.get(url)
    assert served.status_code == 200 and served.content == PNG

    p = await client.post(f"{BASE}/products", headers=shop["headers"], json={"name": "With picture", "image_url": url, "variants": [{"price": "10.00"}]})
    assert p.status_code == 201 and p.json()["image_url"] == url
    # the picture is optional, and can be removed again
    gone = await client.patch(f"{BASE}/products/{p.json()['id']}", headers=shop["headers"], json={"image_url": ""})
    assert gone.status_code == 200 and gone.json()["image_url"] == ""
    plain = await client.post(f"{BASE}/products", headers=shop["headers"], json={"name": "No picture", "variants": [{"price": "10.00"}]})
    assert plain.status_code == 201 and plain.json()["image_url"] == ""


async def test_formats_are_recognised_by_content_not_by_name(client, make_tenant):
    shop = await make_tenant("local_shop")
    assert (await upload(client, shop["headers"], JPG, "photo.png")).json()["url"].endswith(".jpg")
    assert (await upload(client, shop["headers"], WEBP, "p.webp", "image/webp")).json()["url"].endswith(".webp")
    for bad in (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", b"MZ\x90\x00 not an image", b""):
        r = await upload(client, shop["headers"], bad, "evil.png")
        assert r.status_code == 422 and r.json()["code"] == "unsupported_image"


async def test_size_limit(client, make_tenant, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "max_upload_mb", 1)
    shop = await make_tenant("local_shop")
    r = await upload(client, shop["headers"], PNG + b"\x00" * (1024 * 1024))
    assert r.status_code == 422 and r.json()["code"] == "image_too_large"


async def test_uploading_needs_permission_and_a_login(client, make_tenant):
    shop = await make_tenant("local_shop")
    assert (await upload(client, {}, PNG)).status_code in (401, 403)
    users = await client.post(f"{BASE}/users", headers=shop["headers"], json={"name": "Cash Ier", "phone": "01955500001", "password": "cashier1234", "role": "cashier"})
    assert users.status_code == 201, users.text
    login = await client.post(f"{BASE}/auth/login", json={"phone": "01955500001", "password": "cashier1234"})
    cashier = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await upload(client, cashier, PNG)).status_code == 403


async def test_product_image_url_must_be_an_upload_or_a_web_address(client, make_tenant):
    shop = await make_tenant("local_shop")
    for bad in ("javascript:alert(1)", "/etc/passwd", "../uploads/x.png", "data:image/png;base64,AAAA"):
        r = await client.post(f"{BASE}/products", headers=shop["headers"], json={"name": "Bad", "image_url": bad, "variants": [{"price": "1.00"}]})
        assert r.status_code == 422, bad
    ok = await client.post(f"{BASE}/products", headers=shop["headers"], json={"name": "Ok", "image_url": "https://example.com/a.png", "variants": [{"price": "1.00"}]})
    assert ok.status_code == 201
