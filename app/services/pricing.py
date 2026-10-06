"""The one pricing algorithm.

The till, the server and the tests all evaluate a cart in exactly this order, with half-up rounding
at every step, so a receipt printed offline matches what the server later books to the paisa:

  gross  = unit_price × qty                       (per line, rounded to 2 dp)
  net    = max(gross − line discount, 0)
  bill discount is spread over lines pro rata to `net` (the last line takes the remainder)
  tax    = inclusive: taxable × r ÷ (100 + r)  |  exclusive: taxable × r ÷ 100
  total  = Σ taxable (+ Σ tax when exclusive), then optional cash rounding to the nearest taka

`docs/pricing_vectors.json` holds golden cases generated from this module; the Flutter client's tests
replay them, which is what keeps the two implementations from drifting apart.
"""
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.core.money import q2

ZERO = Decimal("0.00")


@dataclass
class LineInput:
    unit_price: Decimal
    qty: Decimal
    discount: Decimal = ZERO
    tax_rate: Decimal = ZERO


@dataclass
class PricedLine:
    gross: Decimal
    net: Decimal
    bill_discount_share: Decimal
    taxable: Decimal
    tax: Decimal
    total: Decimal


@dataclass
class PricedCart:
    lines: list[PricedLine] = field(default_factory=list)
    subtotal: Decimal = ZERO  # Σ net (after line discounts, before bill discount)
    discount: Decimal = ZERO  # bill-level discount actually applied
    tax_total: Decimal = ZERO
    pre_round_total: Decimal = ZERO
    rounding: Decimal = ZERO
    total: Decimal = ZERO


def price_cart(
    lines: list[LineInput],
    *,
    bill_discount: Decimal = ZERO,
    tax_mode: str = "inclusive",
    cash_rounding: str = "none",
) -> PricedCart:
    grosses = [q2(l.unit_price * l.qty) for l in lines]
    nets = [max(g - min(q2(l.discount), g), ZERO) for g, l in zip(grosses, lines)]
    subtotal = sum(nets, ZERO)
    discount = min(max(q2(bill_discount), ZERO), subtotal)

    shares: list[Decimal] = []
    allocated = ZERO
    for i, net in enumerate(nets):
        if i == len(nets) - 1:
            share = min(max(discount - allocated, ZERO), net)
        elif subtotal > 0:
            share = q2(discount * net / subtotal)
        else:
            share = ZERO
        shares.append(share)
        allocated += share

    priced: list[PricedLine] = []
    for line, gross, net, share in zip(lines, grosses, nets, shares):
        taxable = net - share
        rate = Decimal(line.tax_rate)
        if rate <= 0:
            tax = ZERO
        elif tax_mode == "inclusive":
            tax = q2(taxable * rate / (Decimal(100) + rate))
        else:
            tax = q2(taxable * rate / Decimal(100))
        total = taxable if tax_mode == "inclusive" else taxable + tax
        priced.append(PricedLine(gross, net, share, taxable, tax, total))

    pre_round = sum((p.total for p in priced), ZERO)
    total = pre_round
    rounding = ZERO
    if cash_rounding == "nearest_1":
        total = q2(pre_round.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        rounding = total - pre_round

    return PricedCart(
        lines=priced,
        subtotal=subtotal,
        discount=discount,
        tax_total=sum((p.tax for p in priced), ZERO),
        pre_round_total=pre_round,
        rounding=rounding,
        total=total,
    )
