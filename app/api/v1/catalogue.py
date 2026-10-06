import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Ctx, tenant_ctx
from app.core.errors import NotFoundError
from app.core.permissions import Permission
from app.db.session import get_db
from app.models.catalogue import Barcode, Category, Product, TaxClass, Variant
from app.schemas.catalogue import CategoryIn, ProductIn, ProductPatch, TaxClassIn
from app.services import catalogue as svc
from app.services import serializers
from app.services.common import audit

router = APIRouter(tags=["Catalogue"])

view = tenant_ctx(permission=Permission.view_inventory, feature="inventory", allow_lapsed=True)
manage = tenant_ctx(permission=Permission.manage_products, feature="inventory", write=True)


@router.get("/products", summary="Search the catalogue (server-side, paginated)")
async def list_products(
    q: str = "", category_id: uuid.UUID | None = None, active: bool | None = True,
    attr: str | None = Query(default=None, description="key:value filter on a filterable attribute"),
    limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
    ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db),
) -> dict:
    conds = [Product.tenant_id == ctx.tenant.id]
    if active is not None:
        conds.append(Product.active.is_(active))
    if category_id:
        conds.append(Product.category_id == category_id)
    if q.strip():
        like = f"%{q.strip()}%"
        in_variants = select(Variant.product_id).outerjoin(Barcode, Barcode.variant_id == Variant.id).where(
            Variant.tenant_id == ctx.tenant.id, or_(Variant.sku.ilike(like), Barcode.code == q.strip()))
        conds.append(or_(Product.name.ilike(like), Product.name_local.ilike(like), Product.id.in_(in_variants)))
    if attr and ":" in attr:
        key, _, value = attr.partition(":")
        conds.append(Product.attributes[key].astext == value)
    total = (await db.execute(select(func.count()).select_from(Product).where(*conds))).scalar_one()
    rows = (await db.execute(
        select(Product).options(selectinload(Product.variants).selectinload(Variant.barcodes)).where(*conds)
        .order_by(Product.name).limit(limit).offset(offset)
    )).scalars().all()
    cost = ctx.can("view_cost")
    return {"items": [serializers.product_out(p, include_cost=cost) for p in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/products/lookup", summary="Resolve a scanned barcode, SKU or scale label")
async def lookup(code: str, ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db)) -> dict:
    hit = await svc.lookup(db, ctx.tenant, code)
    if hit is None:
        raise NotFoundError("No item matches that code", code="unknown_code")
    return hit


@router.get("/products/{product_id}")
async def get_product(product_id: uuid.UUID, ctx: Ctx = Depends(view), db: AsyncSession = Depends(get_db)) -> dict:
    return serializers.product_out(await svc.load_product(db, ctx.tenant, product_id), include_cost=ctx.can("view_cost"))


@router.post("/products", status_code=201)
async def create_product(body: ProductIn, ctx: Ctx = Depends(manage), db: AsyncSession = Depends(get_db)) -> dict:
    p = await svc.create_product(db, tenant=ctx.tenant, payload=body)
    await audit(db, action="product.created", actor=ctx.user, tenant_id=ctx.tenant.id, entity="product", entity_id=p.id, summary=p.name,
                impersonated_by=ctx.impersonated_by)
    await db.commit()
    return serializers.product_out(await svc.load_product(db, ctx.tenant, p.id), include_cost=ctx.can("view_cost"))


@router.patch("/products/{product_id}")
async def update_product(product_id: uuid.UUID, body: ProductPatch, ctx: Ctx = Depends(manage), db: AsyncSession = Depends(get_db)) -> dict:
    before = await svc.load_product(db, ctx.tenant, product_id)
    prices = {str(v.id): str(v.price) for v in before.variants}
    p = await svc.update_product(db, tenant=ctx.tenant, product_id=product_id, payload=body)
    changes = {str(v.id): str(v.price) for v in p.variants if prices.get(str(v.id)) not in (None, str(v.price))}
    if changes:
        await audit(db, action="product.price_changed", actor=ctx.user, tenant_id=ctx.tenant.id, entity="product", entity_id=p.id,
                    summary=f"{p.name}: price changed", data={"before": {k: prices[k] for k in changes}, "after": changes},
                    impersonated_by=ctx.impersonated_by)
    await db.commit()
    return serializers.product_out(await svc.load_product(db, ctx.tenant, p.id), include_cost=ctx.can("view_cost"))


@router.delete("/products/{product_id}")
async def delete_product(product_id: uuid.UUID, ctx: Ctx = Depends(manage), db: AsyncSession = Depends(get_db)) -> dict:
    result = await svc.delete_product(db, tenant=ctx.tenant, product_id=product_id)
    await audit(db, action=f"product.{result}", actor=ctx.user, tenant_id=ctx.tenant.id, entity="product", entity_id=product_id)
    await db.commit()
    return {"result": result}


# -------------------------------------------------------------- categories & tax classes


@router.get("/categories")
async def list_categories(ctx: Ctx = Depends(tenant_ctx(allow_lapsed=True)), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Category).where(Category.tenant_id == ctx.tenant.id).order_by(Category.sort_order, Category.name))).scalars().all()
    return [serializers.category_out(c) for c in rows]


@router.post("/categories", status_code=201)
async def create_category(body: CategoryIn, ctx: Ctx = Depends(manage), db: AsyncSession = Depends(get_db)) -> dict:
    c = Category(tenant_id=ctx.tenant.id, name=body.name.strip(), parent_id=body.parent_id, sort_order=body.sort_order, active=body.active)
    db.add(c)
    await db.commit()
    return serializers.category_out(c)


@router.patch("/categories/{category_id}")
async def update_category(category_id: uuid.UUID, body: CategoryIn, ctx: Ctx = Depends(manage), db: AsyncSession = Depends(get_db)) -> dict:
    c = await db.get(Category, category_id)
    if c is None or c.tenant_id != ctx.tenant.id:
        raise NotFoundError("Category not found")
    c.name, c.parent_id, c.sort_order, c.active = body.name.strip(), body.parent_id, body.sort_order, body.active
    await db.commit()
    return serializers.category_out(c)


@router.get("/tax-classes")
async def list_tax_classes(ctx: Ctx = Depends(tenant_ctx(allow_lapsed=True)), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(TaxClass).where(TaxClass.tenant_id == ctx.tenant.id).order_by(TaxClass.rate))).scalars().all()
    return [serializers.tax_class_out(t) for t in rows]


@router.post("/tax-classes", status_code=201)
async def create_tax_class(body: TaxClassIn, ctx: Ctx = Depends(tenant_ctx(permission=Permission.manage_settings, write=True)), db: AsyncSession = Depends(get_db)) -> dict:
    t = TaxClass(tenant_id=ctx.tenant.id, name=body.name.strip(), rate=body.rate, is_default=body.is_default, active=body.active)
    db.add(t)
    await db.commit()
    return serializers.tax_class_out(t)


@router.patch("/tax-classes/{tax_id}")
async def update_tax_class(tax_id: uuid.UUID, body: TaxClassIn, ctx: Ctx = Depends(tenant_ctx(permission=Permission.manage_settings, write=True)), db: AsyncSession = Depends(get_db)) -> dict:
    t = await db.get(TaxClass, tax_id)
    if t is None or t.tenant_id != ctx.tenant.id:
        raise NotFoundError("Tax class not found")
    t.name, t.rate, t.is_default, t.active = body.name.strip(), body.rate, body.is_default, body.active
    await db.commit()
    return serializers.tax_class_out(t)
