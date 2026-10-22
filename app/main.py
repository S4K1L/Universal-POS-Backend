from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import maintenance_gate
from app.api.v1 import auth, billing, catalogue, customers, inventory, orders, public, reports, sales, sync, users, workspace
from app.api.v1.admin import billing as admin_billing
from app.api.v1.admin import catalog as admin_catalog
from app.api.v1.admin import tenants as admin_tenants
from app.core.config import settings
from app.core.errors import DomainError

TAGS = [
    {"name": "Auth", "description": "Phone + password login, refresh rotation."},
    {"name": "Workspace", "description": "The signed-in business: context, settings, branches."},
    {"name": "Sync", "description": "Offline sync: idempotent event push and cursor-based change pull."},
    {"name": "Catalogue", "description": "Products, variants, barcodes — validated against the business type."},
    {"name": "Inventory", "description": "Stock ledger, lots, suppliers, purchases."},
    {"name": "Sales", "description": "Sales, returns, voids, dues, expenses, shifts."},
    {"name": "Orders", "description": "Quotation → order → delivery (document_flow capability)."},
    {"name": "Reports", "description": "Date-bounded SQL aggregates."},
    {"name": "Billing", "description": "A business paying for its subscription."},
    {"name": "Platform Console — Businesses", "description": "Super admin: add a business, change plan, impersonate."},
    {"name": "Platform Console — Configuration", "description": "Super admin: plans & prices, business types, platform settings."},
    {"name": "Platform Console — Billing & Audit", "description": "Super admin: payment channels, review queue, invoices, audit, overview."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.assert_production_safe()
    yield


app = FastAPI(
    title="Universal POS API",
    version="1.0.0",
    description=(
        "Multi-tenant, offline-first POS for any kind of shop. A business's **type** (supershop, furniture, cosmetic, "
        "local shop…) is data that switches **capabilities** on; every endpoint is guarded by three gates — "
        "**plan** (what they pay for) × **capability** (what their business does) × **role** (what the user may do)."
    ),
    openapi_tags=TAGS,
    lifespan=lifespan,
    dependencies=[Depends(maintenance_gate)],
)

app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)


@app.exception_handler(DomainError)
async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code, **exc.extra})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    first = errors[0] if errors else {}
    field = ".".join(str(p) for p in first.get("loc", []) if p not in ("body", "query", "path"))
    msg = f"{field}: {first.get('msg')}" if field else str(first.get("msg", "Invalid request"))
    return JSONResponse(status_code=422, content={"detail": msg, "code": "validation_failed", "errors": jsonable(errors)})


def jsonable(errors: list[dict]) -> list[dict]:
    return [{"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in errors]


@app.get("/health", tags=["Health"], summary="Liveness (also the app's online probe)")
async def health() -> dict:
    return {"status": "ok"}


P = "/api/v1"
for module in (auth, public, workspace, users, catalogue, inventory, customers, sales, orders, sync, reports, billing,
               admin_tenants, admin_catalog, admin_billing):
    app.include_router(module.router, prefix=P)
