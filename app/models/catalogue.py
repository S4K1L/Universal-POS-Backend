import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import SyncTracked, TenantScoped, Timestamps, UUIDPK


class Category(UUIDPK, TenantScoped, Timestamps, SyncTracked, Base):
    __tablename__ = "categories"
    __sync_entity__ = "category"

    name: Mapped[str] = mapped_column(String(120))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(default=True)


class TaxClass(UUIDPK, TenantScoped, Timestamps, SyncTracked, Base):
    __tablename__ = "tax_classes"
    __sync_entity__ = "tax_class"

    name: Mapped[str] = mapped_column(String(80))
    rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    is_default: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)


class Product(UUIDPK, TenantScoped, Timestamps, SyncTracked, Base):
    __tablename__ = "products"
    __sync_entity__ = "product"
    __table_args__ = (
        Index("ix_products_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
        Index("ix_products_attributes", "attributes", postgresql_using="gin", postgresql_ops={"attributes": "jsonb_path_ops"}),
    )

    name: Mapped[str] = mapped_column(String(200))
    name_local: Mapped[str] = mapped_column(String(200), default="")  # e.g. the Bangla name
    description: Mapped[str] = mapped_column(Text, default="")
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    type: Mapped[str] = mapped_column(String(16), default="stockable")  # stockable|service|non_stock
    base_unit: Mapped[str] = mapped_column(String(24), default="pc")
    # [{"unit": "dozen", "factor": 12}, {"unit": "carton", "factor": 144}] — factor = base units per unit
    alt_units: Mapped[list] = mapped_column(JSONB, default=list)
    allow_fraction: Mapped[bool] = mapped_column(default=False)
    tracking: Mapped[str] = mapped_column(String(8), default="none")  # none|lot
    track_expiry: Mapped[bool] = mapped_column(default=False)
    tax_class_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tax_classes.id", ondelete="SET NULL"), nullable=True)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    image_url: Mapped[str] = mapped_column(String(500), default="")
    reorder_level: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    warranty_months: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(default=True)

    variants: Mapped[list["Variant"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="Variant.created_at"
    )


class Variant(UUIDPK, TenantScoped, Timestamps, SyncTracked, Base):
    """The sellable SKU. A product with no options has exactly one (default) variant."""

    __tablename__ = "variants"
    __sync_entity__ = "product"
    __sync_child__ = True
    __table_args__ = (UniqueConstraint("tenant_id", "sku", name="uq_variants_tenant_sku"),)

    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(160), default="")  # "Rose / 30 ml"
    option_values: Mapped[dict] = mapped_column(JSONB, default=dict)
    price: Mapped[Decimal] = mapped_column()
    mrp: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    cost: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    plu: Mapped[str | None] = mapped_column(String(8), nullable=True)  # scale item code
    active: Mapped[bool] = mapped_column(default=True)

    product: Mapped[Product] = relationship(back_populates="variants")
    barcodes: Mapped[list["Barcode"]] = relationship(back_populates="variant", cascade="all, delete-orphan")

    def sync_target(self) -> tuple[str, uuid.UUID]:
        return "product", self.product_id


class Barcode(UUIDPK, TenantScoped, Timestamps, SyncTracked, Base):
    __tablename__ = "barcodes"
    __sync_entity__ = "product"
    __sync_child__ = True
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_barcodes_tenant_code"),)

    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("variants.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str | None] = mapped_column(String(24), nullable=True)  # a carton barcode sells one carton
    qty_per_scan: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("1"))

    variant: Mapped[Variant] = relationship(back_populates="barcodes")

    def sync_target(self) -> tuple[str, uuid.UUID]:
        return "product", self.product_id
