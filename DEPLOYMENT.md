# UrbanAir AI — Deployment Guide

Target architecture:

```
┌─────────────────┐     HTTPS (VITE_API_BASE_URL)     ┌──────────────────────┐
│  Vercel         │  ───────────────────────────────▶ │  Render Web Service  │
│  React + Vite   │                                    │  FastAPI + ML models │
│  (frontend)     │  ◀─────────────────────────────── │  (backend)           │
└─────────────────┘                                    └──────────┬───────────┘
                                                                  │ SQLAlchemy
                                                                   │ (psycopg, SSL)
                                                       ┌──────────▼───────────┐
                                                       │  Supabase Postgres   │
                                                       └──────────▲───────────┘
                                                                  │ INSERT (dedup)
┌───────────────────────────────┐                                 │
│  Render Background Worker     │  collectors → CSV → live sync ──┘
│  aqi_model collectors + sync  │
└───────────────────────────────┘
```

The application works **without** Supabase (CSV-only mode). Supabase adds
persistence: historical AQI, persisted forecasts, source-attribution
snapshots, and enforcement alerts.

---

## 1. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11 – 3.13 | 3.11 recommended for hosted builds (broad wheel support) |
| Node.js | 18+ | frontend |
| PostgreSQL client | optional | to run `database/schema.sql` with `psql` |
| Git | any | for Render/Vercel deploys |

Windows: run everything from **PowerShell**.

---

## 2. Local setup (PowerShell)

### 2a. Backend

```powershell
cd D:\Placement_Projects\urbanair_full_stack\urbanair_backend
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # then edit .env with your values
python -m uvicorn app.main:app --reload --port 8000
```

Check: <http://127.0.0.1:8000/api/health> →
`{"status":"ok",...,"database":{"configured":false}}`

### 2b. Frontend

```powershell
cd D:\Placement_Projects\urbanair_full_stack\urbanair_frontend
npm install
npm run dev            # http://localhost:5173  (proxies /api -> :8000)
```

### 2c. Collectors (live data)

```powershell
cd D:\Placement_Projects\urbanair_full_stack\aqi_model
.\..\urbanair_backend\venv\Scripts\Activate.ps1   # or its own venv
pip install requests pandas python-dotenv
python run_all.py
```

`run_all.py` starts all 4 collectors and copies the CSVs into
`urbanair_backend/` every 5 minutes. If `DATABASE_URL` is set in the
environment, it also pushes readings into Supabase each round.

---

## 3. Create the Supabase project

1. <https://supabase.com/dashboard> → **New project** (choose a region
   close to your Render region, e.g. Singapore).
2. Save the database password somewhere safe.
3. **Project Settings → Database → Connection string** → choose the
   **Session Pooler** tab (the recommended option for the web service and
   for running the CLI scripts):
   - Host: `aws-0-<region>.pooler.supabase.com`
   - Port: `5432` (Session Pooler). The Transaction Pooler uses `6543`.
   - User: `postgres.<project-ref>`
   - Database: `postgres`
   - Keep `?sslmode=require`. **Never** disable certificate verification.
   - Replace the password placeholder with your **database password**
     (Project Settings → Database → Reset database password). This is NOT
     the publishable/anon API key.
   - Prefix the URL with `postgresql://` (not `postgres://`).

Example (Session Pooler):
```
postgresql://postgres.abcd1234:PASSWORD@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require
```

> The publishable key (`sb_publishable_…`) is a browser/REST credential and
> **cannot** be used to connect to PostgreSQL. A `DATABASE_URL` with the
> database password is always required.

### Where to put it

- **Local:** paste it as `DATABASE_URL=` in `urbanair_backend/.env`
  (git-ignored; a commented placeholder is already present there).
- **Production:** set the same `DATABASE_URL` env var in Render
  (never commit it, never place it in the frontend).

---

## 4. Apply the schema

**Option A — SQL Editor (recommended, reviewable):**

1. Supabase Dashboard → **SQL Editor → New query**.
2. Paste the contents of [`database/schema.sql`](database/schema.sql).
3. Run. It is idempotent (`CREATE ... IF NOT EXISTS`) and never drops data.

**Option B — CLI:**

```powershell
cd urbanair_backend
$env:DATABASE_URL="postgresql://postgres...:PASSWORD@...:6543/postgres?sslmode=require"
python -m scripts.apply_schema
```

Both create the six tables (`stations`, `aqi_readings`,
`weather_readings`, `aqi_forecasts`, `pollution_source_estimates`,
`enforcement_alerts`) and seed the 21 monitored stations.

### Verify the connection and whether the schema already exists

Before applying anything, check the current state with the safe self-test
(it never drops or mutates data):

```powershell
cd urbanair_backend
python -m pip install "psycopg[binary]" psycopg2-binary   # required PostgreSQL drivers
python -m scripts.db_selftest           # connect + list tables + row counts
python -m scripts.db_selftest --write-test   # also proves write access (rolled back)
```

The output reports `PASS: connected`, `SCHEMA: APPLIED` or
`SCHEMA: NOT APPLIED`, and per-table row counts. Only apply the schema if
it reports `NOT APPLIED` — it is idempotent either way, but there is no
need to re-run it.

---

## 5. Import historical data (optional)

`aqi_model/processed_aqi_data.csv` (5 cities, hourly, 2019–2024,
~219k rows). Non-destructive, duplicate-safe.

```powershell
cd urbanair_backend
$env:DATABASE_URL="postgresql://..."
python -m scripts.import_historical --csv ..\aqi_model\processed_aqi_data.csv
# validate first without writing:
python -m scripts.import_historical --csv ..\aqi_model\processed_aqi_data.csv --dry-run
# test small:
python -m scripts.import_historical --limit 1000
```

It prints `valid / invalid / inserted / skipped (dupes)`. Re-running
imports nothing new. The original CSV is never modified.

**Which imports are appropriate (and why they can't duplicate):**

| Source file / script | Rows into | `source` tag | Idempotency |
|---|---|---|---|
| `processed_aqi_data.csv` via `scripts.import_historical` | `aqi_readings` (+ `stations`) | `processed_dataset` | `UNIQUE (station_id, observed_at, source)` — re-runs skip existing rows |
| `live_*.csv` via `scripts.sync_live_to_db` | `aqi_readings`, `weather_readings` | `waqi` / `owm` | same unique key per source |

- Import the **historical** CSV once. It only contains the 5 trained
  cities for 2019–2024; it is the correct, appropriate backfill.
- Keep **live sync** (collectors → `sync_live_to_db`) separate; it appends
  new real readings and is also duplicate-safe.
- Because the unique key includes `source`, re-running the same import is
  safe, but do **not** re-import the same data under a *different* source
  tag — that would create intentional duplicate rows.
- Always `--dry-run` first to see `valid / invalid` counts before writing.

### 5a. Validation snapshot (verified)

Read-only `--dry-run` + profile of `aqi_model/processed_aqi_data.csv`:

| Check | Result |
|---|---|
| Valid rows | **219,125** |
| Invalid rows | **0** |
| Cities | 5 (`Delhi`, `Faridabad`, `Ghaziabad`, `Gurgaon`, `Noida`) |
| Rows per city | 43,825 each (even — no gaps vs. header count) |
| Time range | 2019-01-01 → 2024-01-01, hourly |
| Intra-CSV duplicates (`location_name` + `timestamp`) | **0** |
| Import target / tag | `aqi_readings` (+ `stations`), `source='processed_dataset'` |

Because the import is **insert-only** and duplicate-safe, re-running is a
no-op for already-present rows. The only way to create duplicates is to
re-import the same rows under a *different* `source` tag.

### 5b. Backup plan (do this BEFORE the first real import)

The import only *adds* rows and never deletes/updates, so a backup is a
safety net rather than a strict requirement. Still, take one first:

1. **Supabase dashboard → Database → Backups.** Free projects include
   daily backups; confirm one exists / trigger a manual export if offered.
   (Point-in-time recovery is a paid feature — not required here.)
2. **Logical dump (portable, restorable):**
   ```powershell
   # Schema only
   pg_dump "postgresql://postgres.pzdzpmhixzrihlpatrig:<pw>@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require" --schema-only -f schema_backup.sql
   # Everything (data + schema)
   pg_dump "postgresql://postgres.pzdzpmhixzrihlpatrig:<pw>@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require" -f full_backup.sql
   ```
   (`pg_dump` ships with PostgreSQL client tools; the password must be
   URL-encoded — see §10 Troubleshooting.)
3. **Record the pre-import state** so you can prove the import was clean:
   ```powershell
   cd urbanair_backend
   python -m scripts.db_selftest        # prints table row counts (read-only)
   ```
   At the time of writing every table is empty (0 rows), so a "backup" is
   effectively the empty schema — keep `schema_backup.sql` for reference.

**Restore if needed:** `psql "<DATABASE_URL>" -f full_backup.sql` into an
empty project (or `TRUNCATE` + reload if you only need the data). Never
run `DROP`/`DELETE` against the live project to "re-import" — just re-run
the idempotent importer.

### 5c. Demo mode — latest-only storage (recommended)

For the hackathon/demo we deliberately do **not** load the 219k historical
CSV. Instead the DB keeps only the *current* picture:

- **Stations** — all 21 monitoring points (5 NCR cities + hotspots), seeded
  once with `python -m scripts.seed_stations` (idempotent, insert-only).
- **AQI / weather** — exactly one row per station (+ source). Collectors
  **upsert in place** (`scripts.sync_live_to_db`); a newer reading overwrites
  the previous one, so counts stay flat and duplicates can't pile up.
- **Forecasts / source-attribution / enforcement** — auto-pruned to the last
  **7 days** on every write (`FORECAST_RETENTION_DAYS` / `LOG_RETENTION_DAYS`
  in `db/service.py`).

Dashboard, maps, ML logic and all API response shapes are unchanged — the
endpoints just read the single latest row (falling back to CSV when the DB
is empty/unset).

**Inspect before touching production (read-only, safe):**

```powershell
cd urbanair_backend
python -m scripts.demo_storage_report
```

It prints row counts, how many rows an upsert would collapse, and how many
retention rows are older than 7 days. It never writes or deletes.

> **Do not** run the historical importer (§5) for the demo — it would fill
> `aqi_readings` with 219k rows that latest-only mode then has to prune.
> Any destructive cleanup (removing pre-existing history rows) must be
> reviewed from the dry-run report and approved first.

---

## 6. Deploy the backend to Render

1. Push the repo to GitHub (do **not** commit `.env`).
2. Render → **New → Blueprint** → select the repo. `render.yaml` creates
   `urbanair-backend` (web) and `urbanair-collector` (worker).
3. Set the secret env vars in the Render dashboard (`sync: false` ones):
   - `DATABASE_URL` — Supabase pooler URI (`sslmode=require`)
   - `CORS_ORIGINS` — your Vercel URL, e.g. `https://urbanair.vercel.app`
   - `FRONTEND_URL` — same Vercel URL
   - `GROQ_API_KEY`, `SMTP_EMAIL`, `SMTP_APP_PASSWORD` (optional)
4. For `urbanair-collector`, also set `OWM_API_KEY`, `TOMTOM_API_KEY`,
   `WAQI_API_TOKEN`.
5. Deploy. Backend health check path is `/api/health`.

Manual (no blueprint): **New → Web Service**, root dir `urbanair_backend`,
build `pip install -r requirements.txt`, start
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

> `$PORT` is provided by Render — the app must bind to it (the start
> command does). Model files (~9 MB `.pkl`) ship in the repo, so no
> separate model download is required.

---

## 7. Deploy the frontend to Vercel

1. Vercel → **Add New → Project** → import the repo.
2. **Root Directory:** `urbanair_frontend`. Framework preset: **Vite**.
3. Environment variable (Production + Preview):
   - `VITE_API_BASE_URL` = `https://<your-render-backend>.onrender.com/api`
     (include the `/api` suffix).
4. Deploy. `urbanair_frontend/vercel.json` handles SPA routing so deep
   links / refreshes return `index.html`.
5. Copy the Vercel URL into the backend's `CORS_ORIGINS` /
   `FRONTEND_URL` on Render, then redeploy the backend.

> `VITE_*` variables are embedded in the browser bundle. Never place DB
> credentials, the service-role key, or other secrets there.

---

## 8. Keeping data fresh after deployment

The web service only **reads** data. Collection must run separately —
deploying the API does not start collectors.

- **Recommended (free):** use the included GitHub Actions cron at
  `.github/workflows/collect.yml`. It runs the collectors once every 30
  minutes and mirrors the CSVs into Supabase via
  `scripts/sync_live_to_db`. See **`FREE_DEMO_DATA.md`** for setup and
  alternatives (local Task Scheduler, or `aqi_model/run_all.py` on your
  own machine).
- **Optional (paid):** the `urbanair-collector` Render **Background Worker**
  (`python aqi_model/run_all.py`) is defined but commented out in
  `render.yaml`, because Background Workers are not on Render's free tier.
  Uncomment it only if you choose to pay.

No live updates are claimed unless the collector/scheduler is actually
running with valid upstream API keys.

---

## 9. Test the deployed application

```powershell
# backend health + DB connectivity
curl https://<backend>.onrender.com/api/health
curl https://<backend>.onrender.com/api/db/health

# persisted history (200 once data exists, 503 if DB not configured)
curl "https://<backend>.onrender.com/api/aqi/history/Delhi?hours=168&limit=10"
```

Then open the Vercel URL and confirm the Overview map loads, forecasts
render, and the browser Network tab shows calls to the Render backend
(not `localhost`).

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `database.configured=false` | `DATABASE_URL` not set on Render; add it and redeploy. |
| Connection refused / timeout | Use the Supabase **pooler** host and port `6543`; confirm `sslmode=require`. |
| `could not translate host name` | Wrong host, **or** the password contains unescaped reserved characters (`@ : / ? # & [ ] %`). Percent-encode them (see below). |
| Password with `@`/`:` breaks the URL | Encode the password before pasting. PowerShell: `[uri]::EscapeDataString('<your-password>')` → use that value in `DATABASE_URL` (e.g. `@` becomes `%40`). Or reset the DB password to alphanumeric-only. |
| CORS error in browser | Set `CORS_ORIGINS` to the exact Vercel origin (no trailing slash) and redeploy backend. |
| Frontend calls `localhost` | `VITE_API_BASE_URL` missing at **build** time; set it in Vercel and redeploy. |
| 503 on `/api/current/{city}` | No live reading yet — collectors haven't logged. Run/await the worker. |
| 503 on `/api/aqi/history` | DB not configured (expected in CSV-only mode). |
| `psycopg2` build error on install | Use `psycopg2-binary` (already in requirements). |
| `No module named 'psycopg'` in Actions/Render | `DATABASE_URL` uses `postgresql+psycopg://`; install `psycopg[binary]` (already in requirements.txt). |
| Render worker crashes with `ModuleNotFoundError: db` | Run the sync via `python -m scripts.sync_live_to_db` from `urbanair_backend/`. |

---

## 11. Security checklist

- [ ] `.env` files are git-ignored and never committed.
- [ ] No secrets in `VITE_*` variables or frontend code.
- [ ] Supabase connection uses `sslmode=require` (verification never disabled).
- [ ] CORS restricted to the production frontend origin.
- [ ] Database password / service-role key stored only in Render env vars.
- [ ] Rotate any API keys that were ever shared in a zip / chat.
- [ ] Queries are parameterized (SQLAlchemy) — no string-built SQL.

---

## 12. Endpoint reference (added for the DB integration)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness + whether a DB is configured |
| GET | `/api/db/health` | Real DB connectivity check (`SELECT 1`) |
| GET | `/api/aqi/history/{city}?hours=&limit=&offset=` | Paginated persisted AQI history |
| GET | `/api/forecasts/{city}` | Latest persisted forecasts |
| GET | `/api/alerts?status=&limit=&offset=` | Persisted enforcement alerts |

All previously existing endpoints are unchanged.
