# Free-demo data collection

How to keep the deployed demo showing **genuine, fresh** data without paying
for anything. This complements `DEPLOYMENT.md` (which covers the database and
hosting) by focusing on the one thing free tiers make awkward: **continuously
running the CSV collectors.**

## The constraint

The collectors (`aqi_model/*.py`) pull live data from OpenWeatherMap, TomTom
and WAQI and append it to local `live_*.csv` files. The FastAPI backend then
either reads those CSVs directly (local mode) or reads the mirrored rows from
Supabase (deployed mode, via `scripts/sync_live_to_db`).

Render's **free** web service:

- sleeps after ~15 min of inactivity, and
- **cannot** run Background Workers (those require a paid plan).

So on the free tier nothing keeps the collectors running by itself. We solve
that with a free scheduler that wakes up, collects once, and pushes to
Supabase. The web service then serves the persisted rows (with the
`data_origin` markers described in `DEPLOYMENT.md`).

## Option A — GitHub Actions cron (recommended, free)

A ready-to-use workflow is included at `.github/workflows/collect.yml`. It:

1. runs every 30 minutes (and on demand via **Actions → Run workflow**),
2. runs each collector **once** (`--loop` defaults to `0` = run once),
3. mirrors the resulting CSVs into Supabase with the idempotent
   `scripts.sync_live_to_db`.

**One-time setup:** repo → *Settings → Secrets and variables → Actions → New
repository secret*, add:

| Secret | Where it comes from |
|---|---|
| `TOMTOM_API_KEY` | your TomTom key (traffic) |
| `OWM_API_KEY` | OpenWeatherMap key (pollution + weather) |
| `WAQI_API_TOKEN` | WAQI token (ground-station AQI) |
| `DATABASE_URL` | the Supabase **Session Pooler** URL (see `DEPLOYMENT.md` §3) |

No code changes needed. Keep the secret values out of the repo — they are
referenced as `${{ secrets.* }}` only.

Trade-offs: GitHub cron can be delayed a few minutes under load, and it is a
best-effort scheduler (not a real-time stream). For a demo that is fine.

## Option B — your own machine (Windows Task Scheduler / cron)

If your laptop/PC is on during the demo, run the collectors locally and sync.

```powershell
# one round, then push to Supabase:
cd aqi_model
python air_pollution_collector.py
python traffic_collector.py
python weather_collector.py
python waqi_aqi_collector.py
cd ..\urbanair_backend
$env:DATABASE_URL="postgresql://..."   # Session Pooler URL
python -m scripts.sync_live_to_db --csv-dir ..\aqi_model
```

Schedule the above every 30 min with **Task Scheduler** (Windows) or `cron`
(Linux/macOS). This is the lowest-latency free option and also exercises the
real collectors you demo.

## Option C — always-on local loop

Run the existing one-command loop while you present:

```powershell
cd aqi_model
python run_all.py
```

`run_all.py` starts all collectors in `--loop` mode, copies the CSVs into
`urbanair_backend/`, and (if `DATABASE_URL` is set) pushes them to Supabase
every few minutes. Stop with `Ctrl+C`. Good for a live, in-room demo.

## Where the data lands

| Collected by | CSV | DB table | `source` tag |
|---|---|---|---|
| `waqi_aqi_collector.py` | `live_ground_aqi_log.csv` | `aqi_readings` | `waqi` |
| `air_pollution_collector.py` | `live_pollutants_log.csv` | `aqi_readings` | `owm_nowcast` |
| `weather_collector.py` | `weather_current_log.csv` | `weather_readings` | `owm` |
| `traffic_collector.py` | `traffic_log.csv` | (feeds live source attribution) | — |

All writes are **insert-only** and duplicate-safe (unique constraints), so
running any option on a schedule never creates duplicates.

## What each demo screen needs (so you know what to warm up)

| Screen | Needs live CSVs? | If not present, falls back to |
|---|---|---|
| Current AQI (`/api/current`) | yes | latest **stored** observation (marked `stored_observation`) |
| Historical trend (`/api/historical`) | yes | stored observations (e.g. the 2019–2024 import) |
| Forecast | live history | stored `aqi_forecasts` (`database_stored_forecast`) |
| Source attribution | `traffic_log.csv` + `weather_current_log.csv` | stored source snapshots |
| Enforcement | live source attribution | stored `enforcement_alerts` |

**Warm-up tip:** once, with collectors + Supabase configured, call the
deployed/local endpoints (`/api/forecast/<city>`, `/api/source-attribution`,
`/api/enforcement`) so those tables get their first rows. After that the
deployed web service can serve them even when no collector is running.

## Verifying it works

1. After a collection round, check Supabase: `stations`, `aqi_readings` and
   `weather_readings` should gain new rows.
   ```powershell
   cd urbanair_backend
   python -m scripts.db_selftest     # read-only row counts
   ```
2. Hit the deployed API and confirm `data_origin` is `live_measurement`
   (fresh collector data) rather than `stored_observation`.
3. `GET /api/db/health` should report `connected: true`.

## Do not

- Do **not** commit `.env` or the API keys.
- Do **not** run two schedulers at once — it wastes API quota (it will not
  corrupt data, but it is unnecessary).
- Do **not** use a paid Render Background Worker just for the demo; the
  options above are sufficient.
