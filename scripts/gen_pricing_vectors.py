"""Write docs/pricing_vectors.json — golden cases for the shared pricing algorithm.

The backend test replays them against app.services.pricing; the Flutter client's tests replay the same
file against its own implementation (copy the regenerated file to the Flutter repo's docs/ folder).
Regenerate only when the algorithm intentionally changes.
"""
import json
import random
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.pricing import LineInput, price_cart  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "docs" / "pricing_vectors.json"


def case(name, lines, *, bill_discount="0.00", tax_mode="inclusive", cash_rounding="none"):
    cart = price_cart(
        [LineInput(Decimal(l["unit_price"]), Decimal(l["qty"]), Decimal(l.get("discount", "0")), Decimal(l.get("tax_rate", "0"))) for l in lines],
        bill_discount=Decimal(bill_discount), tax_mode=tax_mode, cash_rounding=cash_rounding,
    )
    return {
        "name": name, "tax_mode": tax_mode, "cash_rounding": cash_rounding, "bill_discount": bill_discount,
        "lines": [{"unit_price": l["unit_price"], "qty": l["qty"], "discount": l.get("discount", "0.00"), "tax_rate": l.get("tax_rate", "0")} for l in lines],
        "expected": {
            "subtotal": str(cart.subtotal), "discount": str(cart.discount), "tax_total": str(cart.tax_total),
            "rounding": str(cart.rounding), "total": str(cart.total),
            "lines": [{"net": str(p.net), "share": str(p.bill_discount_share), "tax": str(p.tax), "total": str(p.total)} for p in cart.lines],
        },
    }


def main():
    cases = [
        case("single item, no tax", [{"unit_price": "100.00", "qty": "1"}]),
        case("weighed item", [{"unit_price": "78.00", "qty": "1.250"}]),
        case("inclusive VAT 15%", [{"unit_price": "115.00", "qty": "1", "tax_rate": "15"}], tax_mode="inclusive"),
        case("exclusive VAT 15%", [{"unit_price": "1000.00", "qty": "1", "tax_rate": "15"}], tax_mode="exclusive"),
        case("bill discount spread over lines", [{"unit_price": "33.33", "qty": "3"}, {"unit_price": "10.00", "qty": "1"}, {"unit_price": "7.77", "qty": "2"}], bill_discount="10.00"),
        case("discount larger than subtotal is clamped", [{"unit_price": "20.00", "qty": "1"}], bill_discount="999.00"),
        case("line discount larger than gross is clamped", [{"unit_price": "20.00", "qty": "1", "discount": "50.00"}]),
        case("cash rounding up", [{"unit_price": "271.50", "qty": "1"}], cash_rounding="nearest_1"),
        case("cash rounding down", [{"unit_price": "271.49", "qty": "1"}], cash_rounding="nearest_1"),
        case("half-up on a tie", [{"unit_price": "0.125", "qty": "1"}]) if False else case("half-up on a tie", [{"unit_price": "0.50", "qty": "1.005"}]),
        case("mixed tax rates, exclusive, rounded", [{"unit_price": "199.99", "qty": "2", "tax_rate": "15"}, {"unit_price": "49.50", "qty": "1", "tax_rate": "5"}, {"unit_price": "10.00", "qty": "3", "tax_rate": "0"}],
             bill_discount="12.34", tax_mode="exclusive", cash_rounding="nearest_1"),
        case("empty-value line", [{"unit_price": "0.00", "qty": "1"}, {"unit_price": "10.00", "qty": "1"}], bill_discount="3.00"),
    ]
    rng = random.Random(20261006)
    for i in range(240):
        n = rng.randint(1, 8)
        lines = []
        for _ in range(n):
            price = Decimal(rng.randint(100, 250000)) / 100
            qty = Decimal(rng.randint(1, 12000)) / 1000 if rng.random() < 0.4 else Decimal(rng.randint(1, 9))
            gross = price * qty
            disc = (Decimal(rng.randint(0, int(gross * 30))) / 100).quantize(Decimal("0.01")) if rng.random() < 0.3 else Decimal("0.00")
            lines.append({"unit_price": f"{price:.2f}", "qty": f"{qty:.3f}".rstrip("0").rstrip(".") if "." in f"{qty:.3f}" else f"{qty}",
                          "discount": f"{disc:.2f}", "tax_rate": rng.choice(["0", "0", "5", "7.5", "10", "15"])})
        bd = f"{Decimal(rng.randint(0, 50000)) / 100:.2f}" if rng.random() < 0.5 else "0.00"
        cases.append(case(f"random {i}", lines, bill_discount=bd, tax_mode=rng.choice(["inclusive", "exclusive"]), cash_rounding=rng.choice(["none", "nearest_1"])))
    OUT.write_text(json.dumps({"version": 1, "cases": cases}, indent=1))
    print(f"wrote {len(cases)} cases to {OUT}")


if __name__ == "__main__":
    main()
