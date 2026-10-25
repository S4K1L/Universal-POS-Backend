# Universal POS — Backend

The API behind [Universal POS](https://github.com/S4K1L/Universal-POS): one point-of-sale for every kind of shop in
Bangladesh, offline-first, with subscription prices and limits set from an admin console.

FastAPI · SQLAlchemy 2 (async) · PostgreSQL · Alembic — 116 endpoints, 302 tests.

A business type (supershop, local shop, cosmetics, furniture, pharmacy) is **data, not code**: it switches capabilities
on, and every endpoint is guarded by three gates — **plan** (what the business bought) × **capability** (what its type
does) × **role**. See `docs/` in the app repository for the full architecture.

## Run it

Python 3.12+ and PostgreSQL:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
createdb universalpos
cp .env.example .env            # or write .env: DATABASE_URL=postgresql+asyncpg://localhost/universalpos
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.seed_demo      # platform data + five demo businesses with two weeks of trading
.venv/bin/uvicorn app.main:app --port 8000 # docs at http://localhost:8000/docs
.venv/bin/pytest -q                        # uses the universalpos_test database
```

`docs/pricing_vectors.json` holds the golden pricing cases the tests replay. The Flutter app replays the same file, so if
you regenerate it (`python -m scripts.gen_pricing_vectors`) copy it into the app repository's `docs/` folder.

### Demo accounts (development only)

Password `demo12345` for all shops; the platform admin is `01700000000` / `admin12345`.

| Phone | Business | Type | Role |
|---|---|---|---|
| 01711000001 | Rahim General Store | local shop | owner |
| 01722000001 | Fresh Bazar Superstore | supershop | owner (`…02` manager, `…03`/`…04` cashiers) |
| 01733000001 | Glow & Co Beauty | cosmetics | owner |
| 01744000001 | Nakshi Furniture Showroom | furniture | owner (`…02` salesperson) |
| 01755000001 | Shefa Pharmacy | pharmacy | owner |
| 01700000000 | Platform console | — | super admin |

## Docker (production)

The database is external — point `DATABASE_URL` in `.env` at it. The compose file forces `ENV=production`, so `.env`
must also pass the production checklist below.

```bash
docker compose up -d --build                                # runs migrations, then serves on host port 9000
docker compose run --rm api python -m scripts.seed_platform # first deploy only, with SEED_ADMIN_PASSWORD in .env
```

Set `API_PORT` to publish on a different host port.

`WEB_CONCURRENCY` sets the worker count (default 2). Each worker opens up to 30 database connections.

## Maintenance mode

Switching maintenance on (admin console → Settings) makes every business route answer
`503 {"code": "maintenance", "detail": <your message>}`. Auth, the admin console and `/public/*` stay open, and
`GET /public/status` reports the state — the app polls it and shows a maintenance page.

## Production checklist

- `ENV=production`, a random `JWT_SECRET_KEY` (≥ 32 chars) and explicit `CORS_ORIGINS` — the app refuses to boot otherwise
- `SEED_ADMIN_PASSWORD` set before the first `seed_platform` run; change it after first login
- PostgreSQL with the `pg_trgm` extension available (the migration creates it)
- Terminate TLS in front of the API; the Flutter web build is static files
