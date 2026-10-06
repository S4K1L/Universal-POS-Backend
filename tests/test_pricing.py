import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.pricing import LineInput, price_cart

VECTORS = json.loads((Path(__file__).resolve().parents[1] / "docs" / "pricing_vectors.json").read_text())["cases"]


@pytest.mark.parametrize("case", VECTORS, ids=[c["name"] for c in VECTORS])
def test_golden_vectors(case):
    cart = price_cart(
        [LineInput(Decimal(l["unit_price"]), Decimal(l["qty"]), Decimal(l["discount"]), Decimal(l["tax_rate"])) for l in case["lines"]],
        bill_discount=Decimal(case["bill_discount"]), tax_mode=case["tax_mode"], cash_rounding=case["cash_rounding"],
    )
    e = case["expected"]
    assert (str(cart.subtotal), str(cart.discount), str(cart.tax_total), str(cart.rounding), str(cart.total)) == (
        e["subtotal"], e["discount"], e["tax_total"], e["rounding"], e["total"])
    assert [(str(p.net), str(p.bill_discount_share), str(p.tax), str(p.total)) for p in cart.lines] == [
        (l["net"], l["share"], l["tax"], l["total"]) for l in e["lines"]]


def test_the_bill_discount_is_never_lost_to_rounding():
    cart = price_cart([LineInput(Decimal("33.33"), Decimal("3")), LineInput(Decimal("10.00"), Decimal("1")), LineInput(Decimal("7.77"), Decimal("2"))],
                      bill_discount=Decimal("10.00"))
    assert sum(p.bill_discount_share for p in cart.lines) == cart.discount == Decimal("10.00")


def test_exclusive_tax_is_added_on_top():
    cart = price_cart([LineInput(Decimal("1000.00"), Decimal("1"), tax_rate=Decimal("15"))], tax_mode="exclusive")
    assert cart.tax_total == Decimal("150.00") and cart.total == Decimal("1150.00")


def test_inclusive_tax_is_carved_out_of_the_price():
    cart = price_cart([LineInput(Decimal("115.00"), Decimal("1"), tax_rate=Decimal("15"))], tax_mode="inclusive")
    assert cart.tax_total == Decimal("15.00") and cart.total == Decimal("115.00")
