"""The signed-in business: context, settings, branches, devices."""
import uuid

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Ctx, tenant_ctx
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.permissions import Permission
from app.db.session import get_db
from app.models.tenant import Branch, Device
from app.schemas.common import Strict
from app.schemas.tenant import DeviceRegisterIn, SettingsUpdateIn
from app.services import context as context_service
from app.services import subscription
from app.services.common import audit, next_counter

router = APIRouter(tags=["Workspace"])

read = tenant_ctx(allow_lapsed=True, allow_suspended=True)


@router.get("/tenants/me/context", summary="Everything the app needs to know about this business")
async def my_context(ctx: Ctx = Depends(read), db: AsyncSession = Depends(get_db)) -> dict:
    return await context_service.build_context(db, ctx.tenant, ctx.user)


@router.patch("/tenants/me/settings", summary="Edit business settings")
async def update_settings(body: SettingsUpdateIn, ctx: Ctx = Depends(tenant_ctx(permission=Permission.manage_settings, write=True)),
                          db: AsyncSession = Depends(get_db)) -> dict:
    t = ctx.tenant
    s = dict(t.settings or {})
    d = body.model_dump(exclude_unset=True)
    if "name" in d and d["name"]:
        t.name = d["name"].strip()
    if "address" in d:
        t.address = d["address"] or ""
    if "language" in d:
        s["language"] = d["language"]
    if "timezone" in d:
        s["timezone"] = d["timezone"]
    tax = dict(s.get("tax", {}))
    if "tax_mode" in d:
        tax["mode"] = d["tax_mode"]
    if "cash_rounding" in d:
        tax["cash_rounding"] = d["cash_rounding"]
    s["tax"] = tax
    receipt = dict(s.get("receipt", {}))
    for src, dst in (("receipt_header", "header"), ("receipt_footer", "footer"), ("address", "address"), ("phone", "phone"), ("vat_reg_no", "vat_reg_no")):
        if src in d:
            receipt[dst] = d[src] or ""
    s["receipt"] = receipt
    stock = dict(s.get("stock", {}))
    if "allow_negative_stock" in d:
        stock["allow_negative"] = d["allow_negative_stock"]
    s["stock"] = stock
    loyalty = dict(s.get("loyalty", {}))
    if "loyalty_enabled" in d:
        if d["loyalty_enabled"] and not ctx.has_capability("loyalty"):
            raise ValidationFailed("Loyalty points are not available for this business type", code="capability_disabled")
        loyalty["enabled"] = d["loyalty_enabled"]
    if "loyalty_points_per_100" in d:
        loyalty["points_per_100"] = d["loyalty_points_per_100"]
    s["loyalty"] = loyalty
    if "payment_methods" in d and d["payment_methods"] is not None:
        methods = []
        for m in d["payment_methods"]:
            code = str(m.get("code", "")).strip().lower()
            if not code or not code.replace("_", "").isalnum():
                raise ValidationFailed("Payment method codes must be letters/digits", code="validation_failed")
            methods.append({"code": code, "label": str(m.get("label", code))[:40], "label_bn": str(m.get("label_bn", ""))[:40],
                            "kind": m.get("kind", "other"), "enabled": bool(m.get("enabled", True))})
        if not any(m["code"] == "cash" and m["enabled"] for m in methods):
            raise ValidationFailed("Cash must stay enabled", code="validation_failed")
        s["payment_methods"] = methods
    scale = dict(s.get("scale", {}))
    if "scale_prefix" in d:
        scale["prefix"] = d["scale_prefix"]
    if "scale_mode" in d:
        scale["mode"] = d["scale_mode"]
    s["scale"] = scale
    if "deposit_percent_min" in d:
        s["deposit_percent_min"] = d["deposit_percent_min"]
    t.settings = s
    await audit(db, action="settings.updated", actor=ctx.user, tenant_id=t.id, entity="tenant", entity_id=t.id,
                summary="Business settings changed", data={"fields": sorted(d)}, impersonated_by=ctx.impersonated_by)
    await db.commit()
    return await context_service.build_context(db, t, ctx.user)


# ------------------------------------------------------------------ branches


class BranchIn(Strict):
    name: str = Field(min_length=1, max_length=160)
    code: str = Field(default="", max_length=16)
    address: str = Field(default="", max_length=300)
    phone: str = Field(default="", max_length=32)
    manager_name: str = Field(default="", max_length=160)
    active: bool = True


def branch_out(b: Branch) -> dict:
    return {"id": str(b.id), "name": b.name, "code": b.code, "address": b.address, "phone": b.phone,
            "manager_name": b.manager_name, "is_main": b.is_main, "active": b.active}


@router.get("/branches", summary="Branches (a restricted user only sees their own)")
async def list_branches(ctx: Ctx = Depends(read), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Branch).where(Branch.tenant_id == ctx.tenant.id).order_by(Branch.is_main.desc(), Branch.name))).scalars().all()
    if ctx.user.branch_ids is not None:
        rows = [b for b in rows if str(b.id) in ctx.user.branch_ids]
    return [branch_out(b) for b in rows]


@router.post("/branches", status_code=201)
async def create_branch(body: BranchIn, ctx: Ctx = Depends(tenant_ctx(permission=Permission.manage_branches, feature="multi_branch", write=True)),
                        db: AsyncSession = Depends(get_db)) -> dict:
    await subscription.assert_within_limit(db, ctx.tenant, "branches")
    b = Branch(tenant_id=ctx.tenant.id, name=body.name.strip(), code=(body.code or body.name[:4]).upper(), address=body.address,
               phone=body.phone, manager_name=body.manager_name, active=body.active)
    db.add(b)
    await audit(db, action="branch.created", actor=ctx.user, tenant_id=ctx.tenant.id, entity="branch", entity_id=b.id, summary=b.name)
    await db.commit()
    return branch_out(b)


@router.patch("/branches/{branch_id}")
async def update_branch(branch_id: uuid.UUID, body: BranchIn, ctx: Ctx = Depends(tenant_ctx(permission=Permission.manage_branches, write=True)),
                        db: AsyncSession = Depends(get_db)) -> dict:
    b = await db.get(Branch, branch_id)
    if b is None or b.tenant_id != ctx.tenant.id:
        raise NotFoundError("Branch not found")
    if b.is_main and not body.active:
        raise ConflictError("The main branch cannot be closed", code="main_branch")
    if body.active and not b.active:
        await subscription.assert_within_limit(db, ctx.tenant, "branches")
    b.name, b.address, b.phone, b.manager_name, b.active = body.name.strip(), body.address, body.phone, body.manager_name, body.active
    if body.code:
        b.code = body.code.upper()
    await db.commit()
    return branch_out(b)


# ------------------------------------------------------------------ devices


def device_out(d: Device) -> dict:
    return {"id": str(d.id), "name": d.name, "device_code": d.device_code, "branch_id": str(d.branch_id), "platform": d.platform,
            "app_version": d.app_version, "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
            "last_sync_at": d.last_sync_at.isoformat() if d.last_sync_at else None, "active": d.active}


@router.post("/devices/register", status_code=201, summary="Register this till; returns the code that prefixes its offline invoice numbers")
async def register_device(body: DeviceRegisterIn, ctx: Ctx = Depends(tenant_ctx(permission=Permission.manage_devices, write=True)),
                          db: AsyncSession = Depends(get_db)) -> dict:
    branch = await db.get(Branch, body.branch_id)
    if branch is None or branch.tenant_id != ctx.tenant.id or not branch.active:
        raise NotFoundError("Branch not found")
    await subscription.assert_within_limit(db, ctx.tenant, "devices")
    taken = {c for (c,) in (await db.execute(select(Device.device_code).where(Device.tenant_id == ctx.tenant.id))).all()}
    while True:  # codes only need to be unique per business; skip any already in use
        n = await next_counter(db, tenant_id=ctx.tenant.id, name="device")
        code = f"D{n:02d}"
        if code not in taken:
            break
    d = Device(tenant_id=ctx.tenant.id, branch_id=branch.id, name=body.name.strip(), device_code=code,
               platform=body.platform, app_version=body.app_version, registered_by=ctx.user.id)
    db.add(d)
    await audit(db, action="device.registered", actor=ctx.user, tenant_id=ctx.tenant.id, entity="device", entity_id=d.id, summary=f"{d.name} ({d.device_code}) at {branch.name}")
    await db.commit()
    return device_out(d)


@router.get("/devices")
async def list_devices(ctx: Ctx = Depends(tenant_ctx(permission=Permission.manage_devices, allow_lapsed=True)), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Device).where(Device.tenant_id == ctx.tenant.id).order_by(Device.device_code))).scalars().all()
    return [device_out(d) for d in rows]


class DevicePatch(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None


@router.patch("/devices/{device_id}")
async def update_device(device_id: uuid.UUID, body: DevicePatch, ctx: Ctx = Depends(tenant_ctx(permission=Permission.manage_devices, write=True)),
                        db: AsyncSession = Depends(get_db)) -> dict:
    d = await db.get(Device, device_id)
    if d is None or d.tenant_id != ctx.tenant.id:
        raise NotFoundError("Device not found")
    if body.name:
        d.name = body.name.strip()
    if body.active is not None:
        if body.active and not d.active:
            await subscription.assert_within_limit(db, ctx.tenant, "devices")
        d.active = body.active
    await db.commit()
    return device_out(d)
