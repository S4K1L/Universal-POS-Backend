import uuid
from datetime import date
from decimal import Decimal

from pydantic import Field

from app.core.money import Money, Qty
from app.schemas.common import Strict


class BarcodeIn(Strict):
    code: str = Field(min_length=1, max_length=64)
    unit: str | None = Field(default=None, max_length=24)
    qty_per_scan: Qty = Field(default=Decimal("1"), gt=0)


class OpeningStockIn(Strict):
    branch_id: uuid.UUID | None = None  # defaults to the main branch
    qty: Qty = Field(gt=0)
    unit_cost: Money | None = None
    lot_no: str = Field(default="OPENING", max_length=64)
    expiry: date | None = None


class VariantIn(Strict):
    id: uuid.UUID | None = None
    sku: str | None = Field(default=None, max_length=64)
    option_values: dict[str, str] = Field(default_factory=dict)
    price: Money = Field(ge=0)
    mrp: Money = Field(default=Decimal("0"), ge=0)
    cost: Money = Field(default=Decimal("0"), ge=0)
    plu: str | None = Field(default=None, pattern=r"^\d{1,8}$")
    active: bool = True
    barcodes: list[BarcodeIn] = Field(default_factory=list, max_length=12)
    opening_stock: OpeningStockIn | None = None


class AltUnit(Strict):
    unit: str = Field(min_length=1, max_length=24)
    factor: Qty = Field(gt=0)


class ProductIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    name_local: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=4000)
    category_id: uuid.UUID | None = None
    type: str = Field(default="stockable", pattern="^(stockable|service|non_stock)$")
    base_unit: str = Field(default="pc", max_length=24)
    alt_units: list[AltUnit] = Field(default_factory=list, max_length=6)
    allow_fraction: bool = False
    tracking: str = Field(default="none", pattern="^(none|lot)$")
    track_expiry: bool = False
    tax_class_id: uuid.UUID | None = None
    attributes: dict = Field(default_factory=dict)
    image_url: str = Field(default="", max_length=500)
    reorder_level: Qty = Field(default=Decimal("0"), ge=0)
    warranty_months: int = Field(default=0, ge=0, le=240)
    variants: list[VariantIn] = Field(min_length=1, max_length=100)


class ProductPatch(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    name_local: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    category_id: uuid.UUID | None = None
    clear_category: bool = False
    base_unit: str | None = Field(default=None, max_length=24)
    alt_units: list[AltUnit] | None = None
    allow_fraction: bool | None = None
    tracking: str | None = Field(default=None, pattern="^(none|lot)$")
    track_expiry: bool | None = None
    tax_class_id: uuid.UUID | None = None
    clear_tax_class: bool = False
    attributes: dict | None = None
    image_url: str | None = Field(default=None, max_length=500)
    reorder_level: Qty | None = Field(default=None, ge=0)
    warranty_months: int | None = Field(default=None, ge=0, le=240)
    active: bool | None = None
    variants: list[VariantIn] | None = None


class CategoryIn(Strict):
    name: str = Field(min_length=1, max_length=120)
    parent_id: uuid.UUID | None = None
    sort_order: int = 0
    active: bool = True


class TaxClassIn(Strict):
    name: str = Field(min_length=1, max_length=80)
    rate: Decimal = Field(ge=0, le=100)
    is_default: bool = False
    active: bool = True
