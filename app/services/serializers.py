"""Wire shapes shared by the REST API and the sync feed. Money/quantity are decimal strings."""
from app.models.catalogue import Category, Product, TaxClass, Variant
from app.models.customers import Customer
from app.models.inventory import StockBalance


def s(value) -> str:
    return str(value)


def variant_out(v: Variant, *, include_cost: bool) -> dict:
    return {
        "id": s(v.id), "sku": v.sku, "label": v.label, "option_values": v.option_values or {},
        "price": s(v.price), "mrp": s(v.mrp), "cost": s(v.cost) if include_cost else None,
        "plu": v.plu, "active": v.active,
        "barcodes": [
            {"id": s(b.id), "code": b.code, "unit": b.unit, "qty_per_scan": s(b.qty_per_scan)} for b in v.barcodes
        ],
    }


def product_out(p: Product, *, include_cost: bool = False) -> dict:
    return {
        "id": s(p.id), "name": p.name, "name_local": p.name_local, "description": p.description,
        "category_id": s(p.category_id) if p.category_id else None, "type": p.type, "base_unit": p.base_unit,
        "alt_units": p.alt_units or [], "allow_fraction": p.allow_fraction, "tracking": p.tracking,
        "track_expiry": p.track_expiry, "tax_class_id": s(p.tax_class_id) if p.tax_class_id else None,
        "attributes": p.attributes or {}, "image_url": p.image_url, "reorder_level": s(p.reorder_level),
        "warranty_months": p.warranty_months, "active": p.active,
        "variants": [variant_out(v, include_cost=include_cost) for v in p.variants],
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def category_out(c: Category) -> dict:
    return {
        "id": s(c.id), "name": c.name, "parent_id": s(c.parent_id) if c.parent_id else None,
        "sort_order": c.sort_order, "active": c.active,
    }


def tax_class_out(t: TaxClass) -> dict:
    return {"id": s(t.id), "name": t.name, "rate": s(t.rate), "is_default": t.is_default, "active": t.active}


def customer_out(c: Customer) -> dict:
    return {
        "id": s(c.id), "name": c.name, "phone": c.phone, "address": c.address, "group": c.group,
        "credit_limit": s(c.credit_limit), "balance": s(c.balance), "points": c.points, "notes": c.notes,
        "attributes": c.attributes or {}, "active": c.active,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def stock_out(b: StockBalance) -> dict:
    return {"id": s(b.id), "variant_id": s(b.variant_id), "branch_id": s(b.branch_id), "on_hand": s(b.on_hand)}
