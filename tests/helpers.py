import uuid
from decimal import Decimal

from tests.conftest import new_id, now_iso


def product_body(name="Test Item", price="100.00", cost="60.00", **extra) -> dict:
    body = {
        "name": name, "variants": [{"price": price, "cost": cost, **extra.pop("variant", {})}],
        **extra,
    }
    return body


async def create_product(client, h, **kw) -> dict:
    r = await client.post("/api/v1/products", json=product_body(**kw), headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def sale_event(shop: dict, lines: list[dict], *, payments=None, customer_id=None, invoice_no=None, bill_discount="0.00",
               tax_mode="inclusive", cash_rounding="none", totals=None, event_id=None, sale_id=None) -> dict:
    sale_id = sale_id or new_id()
    payload = {
        "id": sale_id, "invoice_no": invoice_no or f"INV-TEST-{uuid.uuid4().hex[:6].upper()}",
        "sold_at": now_iso(), "branch_id": shop["branch_id"], "customer_id": customer_id, "customer_name": "Walk-in",
        "lines": [{"id": new_id(), **l} for l in lines], "bill_discount": bill_discount, "tax_mode": tax_mode,
        "cash_rounding": cash_rounding,
        "payments": payments if payments is not None else None,
    }
    if totals:
        payload["totals"] = totals
    return {"id": event_id or new_id(), "type": "sale.create", "occurred_at": now_iso(), "payload": payload}


def line(variant_id, qty="1", price="100.00", **extra) -> dict:
    return {"variant_id": variant_id, "qty": qty, "unit_price": price, **extra}


def pay(amount, method="cash") -> dict:
    return {"id": new_id(), "method": method, "amount": str(amount)}


async def push(client, shop, events: list[dict], headers=None) -> list[dict]:
    r = await client.post("/api/v1/sync/push", json={"events": events}, headers=headers or shop["headers"])
    assert r.status_code == 200, r.text
    return r.json()["results"]
