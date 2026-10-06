import re
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import CapabilityDisabled, ConflictError, NotFoundError, ValidationFailed
from app.models.catalogue import Barcode, Category, Product, TaxClass, Variant
from app.models.sales import SaleLine
from app.models.tenant import Branch, Tenant
from app.schemas.catalogue import ProductIn, ProductPatch, VariantIn
from app.services import inventory, subscription
from app.services.common import next_number


# ------------------------------------------------------------------ attribute validation


def validate_attributes(definitions: list[dict], values: dict) -> dict:
    """Check a product's dynamic attributes against the business type's definitions.

    Unknown keys are dropped (so a field removed from the template doesn't linger), required fields must be
    present, and each value must fit its declared type. Attributes describe — they never drive logic.
    """
    cleaned: dict = {}
    problems: list[str] = []
    for d in definitions:
        key, label, kind = d["key"], d.get("label", d["key"]), d.get("type", "text")
        raw = values.get(key)
        empty = raw is None or raw == "" or raw == []
        if empty:
            if d.get("required"):
                problems.append(f"{label} is required")
            continue
        try:
            if kind in ("text", "textarea", "select_or_create", "color"):
                cleaned[key] = str(raw).strip()[:2000]
            elif kind == "select":
                if str(raw) not in d.get("options", []):
                    raise ValueError(f"must be one of {', '.join(d.get('options', []))}")
                cleaned[key] = str(raw)
            elif kind == "multiselect":
                items = raw if isinstance(raw, list) else [raw]
                bad = [i for i in items if str(i) not in d.get("options", [])]
                if bad:
                    raise ValueError(f"unknown option {bad[0]}")
                cleaned[key] = [str(i) for i in items]
            elif kind == "int":
                cleaned[key] = int(raw)
            elif kind in ("decimal", "money"):
                cleaned[key] = str(Decimal(str(raw)))
            elif kind == "bool":
                cleaned[key] = bool(raw) if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes")
            elif kind == "date":
                cleaned[key] = date.fromisoformat(str(raw)[:10]).isoformat()
            else:
                cleaned[key] = raw
        except (ValueError, InvalidOperation) as exc:
            problems.append(f"{label}: {exc}")
    if problems:
        raise ValidationFailed("; ".join(problems), code="invalid_attributes", extra={"problems": problems})
    return cleaned


def _variant_label(option_values: dict[str, str], axes: list[dict]) -> str:
    order = [a["key"] for a in axes]
    parts = [option_values[k] for k in order if option_values.get(k)]
    parts += [v for k, v in option_values.items() if k not in order and v]
    return " / ".join(parts)


async def _check_capabilities(tenant: Tenant, caps: set[str], p: ProductIn | ProductPatch) -> None:
    def need(cap: str, what: str):
        if cap not in caps:
            raise CapabilityDisabled(f"{what} is not enabled for this business", code="capability_disabled", extra={"capability": cap})

    if getattr(p, "tracking", None) == "lot":
        need("lot_tracking", "Batch tracking")
    if getattr(p, "track_expiry", None):
        need("expiry_tracking", "Expiry tracking")
    if getattr(p, "allow_fraction", None):
        need("fractional_qty", "Selling by weight or length")
    if getattr(p, "alt_units", None):
        need("multi_uom", "Multiple units")
    if getattr(p, "warranty_months", 0):
        need("warranty", "Warranty")
    if p.variants is not None and any(v.option_values for v in p.variants):
        need("variants", "Variants")
    if p.variants is not None and len(p.variants) > 1:
        need("variants", "Variants")
    if any(v.plu for v in (p.variants or [])):
        need("scale_barcode", "Scale barcodes")


async def _next_sku(session: AsyncSession, tenant: Tenant, name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]", "", name.upper())[:4] or "ITM"
    return await next_number(session, tenant_id=tenant.id, name="sku", prefix=base, width=5)


async def _apply_variants(session: AsyncSession, tenant: Tenant, product: Product, variants: list[VariantIn], axes: list[dict],
                          *, main_branch: Branch | None, replace: bool) -> None:
    existing = {
        v.id: v for v in (await session.execute(select(Variant).where(Variant.product_id == product.id))).scalars()
    }
    kept: set[uuid.UUID] = set()
    for vin in variants:
        label = _variant_label(vin.option_values, axes)
        if vin.id and vin.id in existing:
            v = existing[vin.id]
            kept.add(v.id)
        else:
            v = Variant(tenant_id=tenant.id, product_id=product.id, sku="", price=vin.price)
            session.add(v)
        v.option_values, v.label = vin.option_values, label
        v.price, v.mrp, v.cost, v.plu, v.active = vin.price, vin.mrp, vin.cost, vin.plu, vin.active
        v.sku = (vin.sku or v.sku or await _next_sku(session, tenant, product.name)).strip()
        await session.flush()
        # barcodes: make the stored set equal the requested set
        have = {b.code: b for b in (await session.execute(select(Barcode).where(Barcode.variant_id == v.id))).scalars()}
        wanted = {b.code.strip(): b for b in vin.barcodes if b.code.strip()}
        for code, b in have.items():
            if code not in wanted:
                await session.delete(b)
        for code, bin_ in wanted.items():
            clash = (await session.execute(
                select(Barcode.id).where(Barcode.tenant_id == tenant.id, Barcode.code == code, Barcode.variant_id != v.id)
            )).first()
            if clash:
                raise ConflictError(f"Barcode {code} is already used by another item", code="barcode_taken")
            if code in have:
                have[code].unit, have[code].qty_per_scan = bin_.unit, bin_.qty_per_scan
            else:
                session.add(Barcode(tenant_id=tenant.id, product_id=product.id, variant_id=v.id, code=code, unit=bin_.unit, qty_per_scan=bin_.qty_per_scan))
        if vin.opening_stock and product.type == "stockable" and main_branch is not None:
            os_ = vin.opening_stock
            branch_id = os_.branch_id or main_branch.id
            await inventory.receive_stock(
                session, tenant_id=tenant.id, branch_id=branch_id, product=product, variant=v, qty=os_.qty,
                unit_cost=os_.unit_cost if os_.unit_cost is not None else vin.cost, reason="opening", lot_no=os_.lot_no,
                expiry=os_.expiry, ref_type="opening", update_average_cost=False,
            )
    if replace:
        for vid, v in existing.items():
            if vid not in kept:
                v.active = False  # sold variants can't be deleted; hiding keeps the history intact


async def create_product(session: AsyncSession, *, tenant: Tenant, payload: ProductIn) -> Product:
    await subscription.assert_within_limit(session, tenant, "products")
    plan = await subscription.get_plan(session, tenant)
    caps = set(subscription.effective_capabilities(tenant, plan))
    await _check_capabilities(tenant, caps, payload)
    settings = tenant.settings or {}
    attributes = validate_attributes(settings.get("attributes", []), payload.attributes)

    if payload.track_expiry and payload.tracking != "lot":
        raise ValidationFailed("Expiry tracking needs batch tracking", code="validation_failed")
    if payload.type != "stockable" and payload.tracking != "none":
        raise ValidationFailed("Only stocked items can be batch-tracked", code="validation_failed")
    await _check_refs(session, tenant, payload.category_id, payload.tax_class_id)

    product = Product(
        tenant_id=tenant.id, name=payload.name.strip(), name_local=payload.name_local.strip(), description=payload.description,
        category_id=payload.category_id, type=payload.type, base_unit=payload.base_unit,
        alt_units=[{"unit": a.unit, "factor": str(a.factor)} for a in payload.alt_units], allow_fraction=payload.allow_fraction,
        tracking=payload.tracking, track_expiry=payload.track_expiry, tax_class_id=payload.tax_class_id,
        attributes=attributes, image_url=payload.image_url, reorder_level=payload.reorder_level,
        warranty_months=payload.warranty_months,
    )
    session.add(product)
    await session.flush()
    main = (await session.execute(select(Branch).where(Branch.tenant_id == tenant.id, Branch.is_main.is_(True)))).scalar_one_or_none()
    try:
        await _apply_variants(session, tenant, product, payload.variants, settings.get("variant_axes", []), main_branch=main, replace=False)
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("SKU or barcode already exists", code="duplicate") from exc
    return await load_product(session, tenant, product.id)


async def _check_refs(session: AsyncSession, tenant: Tenant, category_id, tax_class_id) -> None:
    if category_id:
        c = await session.get(Category, category_id)
        if c is None or c.tenant_id != tenant.id:
            raise NotFoundError("Category not found")
    if tax_class_id:
        t = await session.get(TaxClass, tax_class_id)
        if t is None or t.tenant_id != tenant.id:
            raise NotFoundError("Tax class not found")


async def load_product(session: AsyncSession, tenant: Tenant, product_id: uuid.UUID) -> Product:
    product = (await session.execute(
        select(Product).options(selectinload(Product.variants).selectinload(Variant.barcodes))
        .where(Product.id == product_id, Product.tenant_id == tenant.id).execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if product is None:
        raise NotFoundError("Product not found")
    return product


async def update_product(session: AsyncSession, *, tenant: Tenant, product_id: uuid.UUID, payload: ProductPatch) -> Product:
    product = await load_product(session, tenant, product_id)
    plan = await subscription.get_plan(session, tenant)
    caps = set(subscription.effective_capabilities(tenant, plan))
    await _check_capabilities(tenant, caps, payload)
    settings = tenant.settings or {}
    data = payload.model_dump(exclude_unset=True)

    if "attributes" in data and data["attributes"] is not None:
        product.attributes = validate_attributes(settings.get("attributes", []), data["attributes"])
    for field in ("name", "name_local", "description", "base_unit", "allow_fraction", "tracking", "track_expiry", "image_url", "reorder_level", "warranty_months"):
        if field in data and data[field] is not None:
            setattr(product, field, data[field].strip() if isinstance(data[field], str) and field == "name" else data[field])
    if payload.alt_units is not None:
        product.alt_units = [{"unit": a.unit, "factor": str(a.factor)} for a in payload.alt_units]
    if payload.clear_category:
        product.category_id = None
    elif payload.category_id:
        await _check_refs(session, tenant, payload.category_id, None)
        product.category_id = payload.category_id
    if payload.clear_tax_class:
        product.tax_class_id = None
    elif payload.tax_class_id:
        await _check_refs(session, tenant, None, payload.tax_class_id)
        product.tax_class_id = payload.tax_class_id
    if payload.active is not None:
        if payload.active and not product.active:
            await subscription.assert_within_limit(session, tenant, "products")
        product.active = payload.active
    if product.track_expiry and product.tracking != "lot":
        raise ValidationFailed("Expiry tracking needs batch tracking", code="validation_failed")
    if payload.variants is not None:
        main = (await session.execute(select(Branch).where(Branch.tenant_id == tenant.id, Branch.is_main.is_(True)))).scalar_one_or_none()
        try:
            await _apply_variants(session, tenant, product, payload.variants, settings.get("variant_axes", []), main_branch=main, replace=True)
        except IntegrityError as exc:
            raise ConflictError("SKU or barcode already exists", code="duplicate") from exc
    await session.flush()
    return await load_product(session, tenant, product.id)


async def delete_product(session: AsyncSession, *, tenant: Tenant, product_id: uuid.UUID) -> str:
    """Hard-delete only when nothing refers to it; otherwise deactivate so history stays readable."""
    product = await load_product(session, tenant, product_id)
    sold = (await session.execute(select(func.count()).select_from(SaleLine).where(SaleLine.product_id == product.id))).scalar_one()
    if sold:
        product.active = False
        return "deactivated"
    try:
        async with session.begin_nested():
            await session.delete(product)
            await session.flush()
        return "deleted"
    except IntegrityError:
        product.active = False
        return "deactivated"


# ------------------------------------------------------------------ lookup (barcode / SKU / scale label)


def parse_scale_barcode(code: str, prefix: str = "20", mode: str = "weight") -> tuple[str, Decimal] | None:
    """EAN-13 variable-measure label printed by a deli/produce scale:

        <prefix 1-2 digits><item code up to 7 digits> <5-digit weight in grams, or price in paisa> <check digit>

    i.e. digits 8–12 carry the measure and the item code is whatever sits between the prefix and digit 8.
    """
    if len(code) != 13 or not code.isdigit() or not prefix or not code.startswith(prefix):
        return None
    plu = code[len(prefix): 7]
    raw = int(code[7:12])
    return plu, (Decimal(raw) / Decimal(1000) if mode == "weight" else Decimal(raw) / Decimal(100))


async def lookup(session: AsyncSession, tenant: Tenant, code: str) -> dict | None:
    code = code.strip()
    if not code:
        return None
    opts = selectinload(Variant.barcodes)
    row = (await session.execute(
        select(Variant, Barcode).join(Barcode, Barcode.variant_id == Variant.id)
        .where(Barcode.tenant_id == tenant.id, Barcode.code == code, Variant.active.is_(True)).options(opts)
    )).first()
    if row:
        return {"variant_id": str(row[0].id), "product_id": str(row[0].product_id), "unit": row[1].unit, "qty": str(row[1].qty_per_scan)}
    row = (await session.execute(
        select(Variant).where(Variant.tenant_id == tenant.id, Variant.sku == code, Variant.active.is_(True))
    )).scalar_one_or_none()
    if row:
        return {"variant_id": str(row.id), "product_id": str(row.product_id), "unit": None, "qty": "1"}
    scale = (tenant.settings or {}).get("scale", {})
    parsed = parse_scale_barcode(code, scale.get("prefix", "20"), scale.get("mode", "weight"))
    if parsed:
        plu, value = parsed
        v = (await session.execute(
            select(Variant).where(Variant.tenant_id == tenant.id, Variant.plu.in_([plu, plu.lstrip("0")]), Variant.active.is_(True))
        )).scalar_one_or_none()
        if v:
            qty = value if scale.get("mode", "weight") == "weight" else (value / v.price if v.price else Decimal(0))
            return {"variant_id": str(v.id), "product_id": str(v.product_id), "unit": None, "qty": str(qty.quantize(Decimal("0.001")))}
    return None
