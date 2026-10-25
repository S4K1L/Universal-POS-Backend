"""Seed platform data only — business types, plans, payment channels, settings and the first super admin. Idempotent.

    SEED_ADMIN_PASSWORD=... .venv/bin/python -m scripts.seed_platform

In production no admin is created unless SEED_ADMIN_PASSWORD is set (SEED_ADMIN_PHONE defaults to 01700000000).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.db.session import SessionLocal, engine  # noqa: E402
from app.seeds.platform import seed_platform  # noqa: E402


async def main() -> None:
    async with SessionLocal() as session:
        created = await seed_platform(session)
    await engine.dispose()
    print("Platform:", created)
    if not created["admin"]:
        print("No admin created (one already exists, or SEED_ADMIN_PASSWORD is not set).")


if __name__ == "__main__":
    asyncio.run(main())
