"""Platform seed: business types, plans, payment channels, settings, the first super admin. Idempotent."""
import os

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_secret
from app.models.identity import User
from app.models.platform import BusinessType, PaymentChannel, Plan, PlatformSetting
from app.seeds.business_types import BUSINESS_TYPES

ALL_FEATURES = ["pos", "inventory", "purchases", "customers", "reports", "reports_advanced", "offline_sync",
                "multi_branch", "staff", "expenses", "export", "orders"]
STARTER = ["pos", "inventory", "purchases", "customers", "reports", "offline_sync", "staff", "expenses"]

# Placeholder prices (BDT). The super admin changes these from the console — they are not in code paths.
PLANS = [
    dict(key="starter", name="Starter", tagline="One shop, one or two tills", monthly_price=999, yearly_price=9990, trial_days=14,
         limits={"products": 1000, "staff": 3, "branches": 1, "devices": 2, "sales_per_month": -1}, features=STARTER, sort_order=1),
    dict(key="business", name="Business", tagline="Growing shops with several staff and branches", monthly_price=1999, yearly_price=19990, trial_days=14,
         limits={"products": 10000, "staff": 10, "branches": 3, "devices": 6, "sales_per_month": -1}, features=ALL_FEATURES, highlight=True, sort_order=2),
    dict(key="enterprise", name="Enterprise", tagline="Chains and large showrooms — no limits", monthly_price=4999, yearly_price=49990, trial_days=14,
         limits={"products": -1, "staff": -1, "branches": -1, "devices": -1, "sales_per_month": -1}, features=ALL_FEATURES, sort_order=3),
]

CHANNELS = [
    dict(kind="bkash", label="bKash (demo — replace)", account_name="Universal POS", account_number="01700000000", wallet_type="merchant",
         instructions="Send money to this number, then paste the transaction ID (TrxID) into the app.", sort_order=1),
    dict(kind="nagad", label="Nagad (demo — replace)", account_name="Universal POS", account_number="01700000001", wallet_type="merchant",
         instructions="Send money to this number, then paste the transaction ID into the app.", sort_order=2),
    dict(kind="bank", label="Bank transfer (demo — replace)", account_name="Universal POS Ltd", account_number="0000000000000", bank_name="Example Bank",
         branch_name="Dhaka Main", routing_number="000000000", instructions="Transfer the exact amount and enter the reference number.", sort_order=3),
]


async def seed_platform(session: AsyncSession) -> dict:
    created = {"business_types": 0, "plans": 0, "channels": 0, "admin": False}
    for spec in BUSINESS_TYPES:
        existing = (await session.execute(select(BusinessType).where(BusinessType.key == spec["key"]))).scalar_one_or_none()
        if existing is None:
            session.add(BusinessType(key=spec["key"], label=spec["label"], description=spec["description"], family=spec["family"],
                                     sort_order=spec["sort_order"], definition=spec["definition"], version=1))
            created["business_types"] += 1
    for spec in PLANS:
        if (await session.execute(select(Plan).where(Plan.key == spec["key"]))).scalar_one_or_none() is None:
            session.add(Plan(**spec))
            created["plans"] += 1
    if (await session.execute(select(func.count()).select_from(PaymentChannel))).scalar_one() == 0:
        for spec in CHANNELS:
            session.add(PaymentChannel(**spec))
            created["channels"] += 1
    for key, value in (("grace_days", {"days": 3}), ("support", {"phone": "", "email": "", "whatsapp": ""}), ("maintenance", {"enabled": False, "message": ""})):
        if await session.get(PlatformSetting, key) is None:
            session.add(PlatformSetting(key=key, value=value))

    admin_phone = os.environ.get("SEED_ADMIN_PHONE", "01700000000")
    admin_password = os.environ.get("SEED_ADMIN_PASSWORD")
    if admin_password is None and not settings.is_production:
        admin_password = "admin12345"  # development only
    if admin_password and (await session.execute(select(User).where(User.phone == admin_phone))).scalar_one_or_none() is None:
        session.add(User(tenant_id=None, name="Platform Admin", phone=admin_phone, password_hash=hash_secret(admin_password), role="super_admin"))
        created["admin"] = True
    await session.commit()
    return created
