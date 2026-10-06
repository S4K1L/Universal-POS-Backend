import os

os.environ["DATABASE_URL"] = "postgresql+asyncpg://localhost/universalpos_test"
os.environ["ENV"] = "test"

import uuid  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401
from app.main import app  # noqa: E402
from app.db import session as db_session  # noqa: E402
from app.seeds.platform import seed_platform  # noqa: E402

ADMIN_PHONE, ADMIN_PASSWORD = "01700000000", "admin12345"


@pytest_asyncio.fixture(scope="session")
async def engine():
    eng = create_async_engine(settings.database_url, pool_size=10, max_overflow=20)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)
    # point the app at the test engine
    db_session.engine = eng
    db_session.SessionLocal = async_sessionmaker(eng, expire_on_commit=False)
    async with db_session.SessionLocal() as s:
        await seed_platform(s)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    async with db_session.SessionLocal() as s:
        yield s


@pytest_asyncio.fixture
async def client(engine):
    async def override():
        async with db_session.SessionLocal() as s:
            yield s

    app.dependency_overrides[db_session.get_db] = override
    from app.api.v1.auth import _FAILS

    _FAILS.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def login(client: AsyncClient, phone: str, password: str) -> dict:
    r = await client.post("/api/v1/auth/login", json={"phone": phone, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def admin(client):
    data = await login(client, ADMIN_PHONE, ADMIN_PASSWORD)
    return auth(data["access_token"])


_phone_counter = [0]


def next_phone() -> str:
    _phone_counter[0] += 1
    return f"0191{_phone_counter[0]:07d}"


@pytest_asyncio.fixture
async def make_tenant(client, admin):
    """Provision a business of the given type through the real admin endpoint; returns a handle with tokens."""

    async def _make(business_type="supershop", plan="enterprise", sample_data=False, **extra) -> dict:
        phone = next_phone()
        body = {"name": f"Test {business_type} {phone[-4:]}", "owner_name": "Owner Test", "phone": phone, "password": "owner12345",
                "business_type_key": business_type, "plan_key": plan, "billing_cycle": "monthly", "sample_data": sample_data, **extra}
        r = await client.post("/api/v1/admin/tenants", json=body, headers=admin)
        assert r.status_code == 201, r.text
        tenant = r.json()
        session = await login(client, phone, "owner12345")
        ctx = session["context"]
        main = next(b for b in ctx["branches"] if b["is_main"])
        h = auth(session["access_token"])
        dr = await client.post("/api/v1/devices/register", json={"name": "Till 1", "branch_id": main["id"], "platform": "test"}, headers=h)
        assert dr.status_code == 201, dr.text
        return {"tenant": tenant, "phone": phone, "headers": h, "context": ctx, "branch_id": main["id"], "device": dr.json(),
                "device_id": dr.json()["id"], "session": session}

    return _make


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())
