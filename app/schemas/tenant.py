import uuid
from datetime import datetime

from pydantic import Field

from app.schemas.common import Strict


class TenantCreateIn(Strict):
    name: str = Field(min_length=2, max_length=160)
    owner_name: str = Field(min_length=2, max_length=160)
    phone: str = Field(pattern=r"^01[3-9]\d{8}$", description="Bangladesh mobile number — the owner's login")
    email: str = Field(default="", max_length=200)
    password: str | None = Field(default=None, min_length=8, max_length=72, description="Generated and returned once if omitted")
    business_type_key: str
    plan_key: str
    billing_cycle: str = Field(default="monthly", pattern="^(monthly|yearly)$")
    trial_days: int | None = Field(default=None, ge=0, le=365)
    city: str = Field(default="", max_length=120)
    address: str = Field(default="", max_length=300)
    branch_name: str = Field(default="Main branch", max_length=160)
    language: str = Field(default="bn", pattern="^(en|bn)$")
    sample_data: bool = False


class TenantUpdateIn(Strict):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    plan_key: str | None = None
    status: str | None = Field(default=None, pattern="^(trialing|active|suspended|cancelled)$")
    billing_cycle: str | None = Field(default=None, pattern="^(monthly|yearly)$")
    period_end: datetime | None = None
    extend_days: int | None = Field(default=None, ge=1, le=3650)
    capabilities_enabled: list[str] | None = None
    capabilities_disabled: list[str] | None = None
    reason: str = Field(default="", max_length=300)


class SettingsUpdateIn(Strict):
    """Tenant-editable business settings. Unknown keys are refused."""

    name: str | None = Field(default=None, max_length=160)
    language: str | None = Field(default=None, pattern="^(en|bn)$")
    timezone: str | None = Field(default=None, max_length=48)
    tax_mode: str | None = Field(default=None, pattern="^(inclusive|exclusive)$")
    cash_rounding: str | None = Field(default=None, pattern="^(none|nearest_1)$")
    receipt_header: str | None = Field(default=None, max_length=200)
    receipt_footer: str | None = Field(default=None, max_length=300)
    address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=32)
    vat_reg_no: str | None = Field(default=None, max_length=40)
    allow_negative_stock: bool | None = None
    loyalty_enabled: bool | None = None
    loyalty_points_per_100: float | None = Field(default=None, ge=0, le=100)
    payment_methods: list[dict] | None = None
    scale_prefix: str | None = Field(default=None, pattern=r"^\d{1,2}$")
    scale_mode: str | None = Field(default=None, pattern="^(weight|price)$")
    deposit_percent_min: int | None = Field(default=None, ge=0, le=100)


class DeviceRegisterIn(Strict):
    name: str = Field(min_length=1, max_length=120)
    branch_id: uuid.UUID
    platform: str = Field(default="", max_length=32)
    app_version: str = Field(default="", max_length=32)
