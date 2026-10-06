import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Ctx, tenant_ctx
from app.core.errors import NotFoundError
from app.db.session import get_db
from app.models.sync import SyncChange
from app.models.tenant import Device
from app.schemas.sync import PushIn
from app.services import sync as sync_service

router = APIRouter(prefix="/sync", tags=["Sync"])

# Sync is never blocked by subscription state: a till's completed sales are facts and must always reach the books.
gate = tenant_ctx(allow_lapsed=True)
push_gate = tenant_ctx(allow_lapsed=True, allow_suspended=True)  # finished work is handed over even when suspended


@router.post("/push", summary="Apply a batch of device events (idempotent per event id)")
async def push(body: PushIn, ctx: Ctx = Depends(push_gate), db: AsyncSession = Depends(get_db)) -> dict:
    results = await sync_service.push(db, tenant=ctx.tenant, user=ctx.user, payload=body)
    await db.commit()
    return {"results": results}


@router.get("/pull", summary="Changes since a cursor (0 = everything)")
async def pull(device_id: uuid.UUID, cursor: int = Query(default=0, ge=0), limit: int = Query(default=500, ge=1, le=1000),
               ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> dict:
    device = await db.get(Device, device_id)
    if device is None or device.tenant_id != ctx.tenant.id or not device.active:
        raise NotFoundError("This device is not registered", code="unknown_device")
    out = await sync_service.pull(db, tenant=ctx.tenant, user=ctx.user, device=device, cursor=cursor, limit=limit)
    await db.commit()
    return out


@router.get("/status", summary="Newest cursor and server time (cheap reachability probe)")
async def status(ctx: Ctx = Depends(gate), db: AsyncSession = Depends(get_db)) -> dict:
    head = (await db.execute(select(func.coalesce(func.max(SyncChange.seq), 0)).where(SyncChange.tenant_id == ctx.tenant.id))).scalar_one()
    from app.services.common import utcnow

    return {"head": int(head), "server_time": utcnow().isoformat()}
