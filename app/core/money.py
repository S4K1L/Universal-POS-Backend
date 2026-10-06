"""Exact money and quantity arithmetic.

Money is Decimal(14,2) and quantities Decimal(14,3) end to end; the wire format
is a decimal *string* so no client ever round-trips through a binary float.
Rounding is always half-up — the Flutter client uses the same rule on integer
paisa/milli-units so both sides agree to the last paisa.
"""
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import AfterValidator

_TWO = Decimal("0.01")
_THREE = Decimal("0.001")
ZERO = Decimal("0.00")


def q2(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(_TWO, rounding=ROUND_HALF_UP)


def q3(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(_THREE, rounding=ROUND_HALF_UP)


# Pydantic field types: lenient on input, always quantised, serialised as strings.
Money = Annotated[Decimal, AfterValidator(q2)]
Qty = Annotated[Decimal, AfterValidator(q3)]
