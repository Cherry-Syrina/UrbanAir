"""
UrbanAir AI — FastAPI Backend
==============================
Serves the ML models (nowcast + forecast) and geospatial data to the
React dashboard.

Run:
    uvicorn app.main:app --reload --port 8000

Endpoints:
    GET  /api/health
    GET  /api/db/health                            -> database connectivity
    GET  /api/cities
    GET  /api/current/{city}                     -> latest known AQI (simulated "live")
    POST /api/nowcast                             -> AQI from pollutant readings
    GET  /api/forecast/{city}                     -> 24/48/72h forecast from latest known state
    GET  /api/forecasts/{city}                    -> persisted forecasts (Supabase)
    GET  /api/historical/{city}?hours=168         -> live time series for trend charts
    GET  /api/aqi/history/{city}                  -> persisted, paginated AQI history (Supabase)
    GET  /api/alerts                              -> persisted enforcement alerts (Supabase)
    GET  /api/geojson/{layer}                     -> parks | residential | industrial | roads
"""

import json
import logging
import os
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional

import joblib
import numpy as np
import pandas as pd

import forecast_engine
from forecast_engine import AQI_CATEGORY_COLORS, aqi_category_from_value
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import source_attribution as sa
import green_cover as gc
import live_history
from db import service as db_service
from . import alert_generator
from . import notifications
from . import groq_arbiter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("urbanair.api")

BASE_DIR = Path(__file__).resolve().parent.parent


def _normalize_origin(origin: str) -> str:
    """Trim whitespace and trailing slashes from a configured origin.

    Browsers send the ``Origin`` header WITHOUT a trailing slash (e.g.
    ``https://app.vercel.app``), while an env var is easy to set as
    ``https://app.vercel.app/``. CORS middleware matches origins literally, so
    a stray slash or space silently blocks every request. Normalising here
    makes configuration forgiving without loosening security.
    """
    return origin.strip().rstrip("/")


def _cors_origins():
    """Restrict CORS to configured origins in production. Defaults to the
    local Vite dev server only -- never a blanket '*' once origins are set."""
    raw = os.environ.get("CORS_ORIGINS", "")
    origins = [o for o in (_normalize_origin(p) for p in raw.split(",")) if o]
    frontend = _normalize_origin(os.environ.get("FRONTEND_URL", ""))
    if frontend and frontend not in origins:
        origins.append(frontend)
    if not origins:
        origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
    return origins


app = FastAPI(title="UrbanAir AI API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_origin_regex=os.environ.get("CORS_ORIGIN_REGEX") or None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------
# Load artifacts once at startup
# ---------------------------------------------------------------
nowcast_regressor = joblib.load(BASE_DIR / "models" / "aqi_regressor.pkl")
nowcast_classifier = joblib.load(BASE_DIR / "models" / "aqi_classifier.pkl")
nowcast_city_encoder = joblib.load(BASE_DIR / "models" / "city_encoder.pkl")
nowcast_meta = json.load(open(BASE_DIR / "models" / "metadata.json"))

# Forecast models/features now live in forecast_engine (shared with the
# headless sync job so persisted forecasts match the live endpoint exactly).

# NOTE: processed_aqi_data.csv (frozen at Jan 2024) is no longer loaded here.
# /api/forecast and /api/historical now build their input from live_history.py,
# which reads the live WAQI + pollutant logs fresh on every call.

CITY_COORDS = {
    "Delhi": (28.7041, 77.1025),
    "Faridabad": (28.4089, 77.3178),
    "Ghaziabad": (28.6692, 77.4538),
    "Gurgaon": (28.4595, 77.0266),
    "Noida": (28.5355, 77.3910),
}

GEOJSON_FILES = {
    "parks": BASE_DIR / "delhi_ncr_parks.geojson",
    "residential": BASE_DIR / "delhi_ncr_residential.geojson",
    "industrial": BASE_DIR / "delhi_ncr_industrial.geojson",
    "construction": BASE_DIR / "delhi_ncr_construction.geojson",
    "roads": BASE_DIR / "delhi_ncr_major_roads.geojson",
}


# ---------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------
class NowcastRequest(BaseModel):
    city: str
    co: float
    no2: float
    o3: float
    pm10: float
    pm25: float
    so2: float
    timestamp: Optional[str] = None  # ISO format; defaults to now



# ---------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "time": datetime.utcnow().isoformat(),
        "database": {"configured": db_service.is_available()},
    }


@app.get("/api/db/health")
def database_health():
    """Real database health check (issues SELECT 1). Always returns 200 with
    a status body so the frontend/ops can distinguish 'not configured' from
    'configured but unreachable'."""
    return db_service.db_health()


@app.get("/api/aqi/history/{city}")
def aqi_history(city: str, hours: int = 168, limit: int = Query(200, ge=1, le=1000),
                offset: int = Query(0, ge=0)):
    """Paginated AQI history from Supabase. Requires DATABASE_URL; returns
    503 when the database is not configured so callers don't mistake an
    empty list for 'no data exists'."""
    if city not in CITY_COORDS:
        raise HTTPException(404, f"Unknown city: {city}")
    data = db_service.query_aqi_history(city, hours=hours, limit=limit, offset=offset)
    if data is None:
        raise HTTPException(
            503,
            "Database not configured or unavailable. Set DATABASE_URL to "
            "enable persisted AQI history (see DEPLOYMENT.md).",
        )
    return data


@app.get("/api/forecasts/{city}")
def persisted_forecasts(city: str):
    """Most recently persisted forecasts for a city (from Supabase)."""
    if city not in CITY_COORDS:
        raise HTTPException(404, f"Unknown city: {city}")
    data = db_service.query_latest_forecasts(city)
    if data is None:
        raise HTTPException(503, "Database not configured or unavailable.")
    return {"city": city, "count": len(data), "items": data}


@app.get("/api/alerts")
def persisted_alerts(status: Optional[str] = None, limit: int = Query(50, ge=1, le=500),
                     offset: int = Query(0, ge=0)):
    """Persisted enforcement alerts (from Supabase), newest first."""
    data = db_service.query_alerts(status=status, limit=limit, offset=offset)
    if data is None:
        raise HTTPException(503, "Database not configured or unavailable.")
    return {"count": len(data), "limit": limit, "offset": offset,
            "status": status, "items": data}


@app.get("/api/cities")
def get_cities():
    return [
        {"name": name, "lat": lat, "lon": lon}
        for name, (lat, lon) in CITY_COORDS.items()
    ]


LIVE_POLLUTANTS_LOG = BASE_DIR / "live_pollutants_log.csv"
LIVE_GROUND_AQI_LOG = BASE_DIR / "live_ground_aqi_log.csv"


def _load_latest_live_pollutants(city: str):
    """Reads live_pollutants_log.csv (written by aqi_model/air_pollution_collector.py)
    fresh on every call -- no caching -- so a new row shows up without a
    backend restart. Returns None if the file doesn't exist yet or has no
    rows for this city."""
    if not LIVE_POLLUTANTS_LOG.exists():
        return None
    try:
        live_df = pd.read_csv(LIVE_POLLUTANTS_LOG, parse_dates=["timestamp"])
    except (pd.errors.EmptyDataError, ValueError):
        return None
    city_rows = live_df[live_df["location_name"] == city]
    if city_rows.empty:
        return None
    return city_rows.sort_values("timestamp").iloc[-1]


def _load_latest_ground_aqi(city: str):
    """Reads live_ground_aqi_log.csv (written by aqi_model/waqi_aqi_collector.py)
    fresh on every call. This is real CPCB ground-station AQI (via aqicn.org),
    more accurate for a hotspot-heavy city than a regional model estimate.
    Returns None if the file doesn't exist yet or has no rows for this city."""
    if not LIVE_GROUND_AQI_LOG.exists():
        return None
    try:
        live_df = pd.read_csv(LIVE_GROUND_AQI_LOG, parse_dates=["timestamp"])
    except (pd.errors.EmptyDataError, ValueError):
        return None
    city_rows = live_df[live_df["location_name"] == city]
    if city_rows.empty:
        return None
    return city_rows.sort_values("timestamp").iloc[-1]


_POLLUTANT_KEYS = ("co", "no2", "o3", "pm10", "pm25", "so2")


def _current_from_stored(city: str, stored: dict,
                         pollutant_reading: Optional[dict] = None) -> dict:
    """Build a /api/current-style payload from a STORED observation.

    Clearly marked `data_origin="stored_observation"` and `is_estimated=True`
    so a historical row from the 2019-2024 dataset is never presented as a
    live measurement.

    The newest row is frequently an AQI-only feed (WAQI ground AQI) with null
    pollutant columns, while the genuine concentrations live in a separate,
    near-simultaneous row (OWM nowcast). ``pollutant_reading`` is that row, if
    any: concentrations are taken from the AQI row when present, else from it,
    and each value remains a real measurement (never interpolated/fabricated).
    ``pollutants_as_of``/``pollutants_source`` say exactly where they came
    from, so the frontend can label them honestly or show "Data unavailable"."""
    aqi_val = stored.get("aqi")
    category = stored.get("category") or aqi_category_from_value(aqi_val) if aqi_val is not None else None

    has_in_stored = any(stored.get(k) is not None for k in _POLLUTANT_KEYS)
    source = stored if has_in_stored else (pollutant_reading or {})

    return {
        "city": city,
        "arbitration": None,
        "timestamp": datetime.now().isoformat(),
        "data_as_of": stored.get("timestamp"),
        "is_estimated": True,
        "data_origin": "stored_observation",
        "aqi": round(float(aqi_val), 1) if aqi_val is not None else None,
        "category": category,
        "color": AQI_CATEGORY_COLORS.get(category, "#888888"),
        "pollutants": {k: source.get(k) for k in _POLLUTANT_KEYS},
        "pollutants_as_of": source.get("timestamp"),
        "pollutants_source": source.get("source"),
    }


@app.get("/api/current/{city}")
def get_current(city: str):
    """Returns the most recent AQI reading for a city.

    Both live sources are computed when available:
      - WAQI ground-station AQI (aqi_model/waqi_aqi_collector.py) -- real
        CPCB monitor data.
      - Nowcast model prediction from live OWM pollutant concentrations
        (aqi_model/air_pollution_collector.py) -- your trained
        aqi_regressor.pkl / aqi_classifier.pkl.

    If they roughly agree, WAQI is used directly (it's real ground-truth
    when available -- no need to call an LLM to agree with itself). If they
    diverge by more than 15%, Groq is asked to arbitrate which one is more
    plausible given context (pollutant mix, recent trend, model's known
    error rate) -- see groq_arbiter.py for exactly what that can and can't
    tell you. The `arbitration` field in the response is always included so
    the frontend can show it transparently, never silently.

    Fallback: if neither live source is available for this city (e.g. the
    deployed web service has no collector CSVs), the most recent STORED
    observation from Supabase is returned with ``data_origin`` =
    ``"stored_observation"`` and ``is_estimated`` = true, so an old historical
    row is never presented as a live measurement. If there is no stored
    reading either, this returns 503."""
    if city not in CITY_COORDS:
        raise HTTPException(404, f"Unknown city: {city}")

    live_pollutants = _load_latest_live_pollutants(city)
    ground_aqi_row = _load_latest_ground_aqi(city)

    if ground_aqi_row is None and live_pollutants is None:
        # No live collector CSV in this container. Try the stored historical
        # observation from Supabase (clearly marked as such) before giving up.
        stored = db_service.query_latest_reading(city)
        if stored and stored.get("aqi") is not None:
            # The newest row is often an AQI-only feed (WAQI); the genuine
            # concentrations usually sit in a separate near-simultaneous row
            # (OWM nowcast). Merge the two honestly -- see _current_from_stored.
            pollutant_reading = db_service.query_latest_pollutant_reading(city)
            return _current_from_stored(city, stored, pollutant_reading)
        raise HTTPException(
            503,
            f"No live data available yet for {city} and no stored reading in "
            f"the database. Either run aqi_model/run_all.py near this service, "
            f"or import historical data (scripts/import_historical)."
        )

    model_aqi = model_category = None
    if live_pollutants is not None:
        co, no2, o3 = float(live_pollutants["co"]), float(live_pollutants["no2"]), float(live_pollutants["o3"])
        pm10, pm25, so2 = float(live_pollutants["pm10"]), float(live_pollutants["pm25"]), float(live_pollutants["so2"])
        pollutant_ts = live_pollutants["timestamp"]
        model_aqi, model_category = _predict_aqi_from_pollutants(city, co, no2, o3, pm10, pm25, so2, pollutant_ts.to_pydatetime())
        pollutants = {"co": co, "no2": no2, "o3": o3, "pm10": pm10, "pm25": pm25, "so2": so2}
    else:
        pollutant_ts = None
        pollutants = None

    arbitration = None
    if ground_aqi_row is not None and model_aqi is not None:
        waqi_aqi, waqi_category = float(ground_aqi_row["aqi"]), ground_aqi_row["aqi_category"]
        if groq_arbiter.needs_arbitration(waqi_aqi, model_aqi):
            live_hist = live_history.get_live_history()
            city_hist = live_hist[live_hist["location_name"] == city].tail(24)
            recent_trend = [round(float(v), 1) for v in city_hist["aqi"].tolist()]
            arbitration = groq_arbiter.arbitrate(
                city, waqi_aqi, waqi_category, model_aqi, model_category,
                pollutants, nowcast_meta["regression_metrics"][nowcast_meta["best_regressor"]]["MAE"],
                recent_trend,
            )
            aqi_val, category = arbitration["final_aqi"], arbitration["final_category"]
            data_as_of = ground_aqi_row["timestamp"] if arbitration["chosen"] == "waqi" else pollutant_ts
        else:
            aqi_val, category, data_as_of = waqi_aqi, waqi_category, ground_aqi_row["timestamp"]
    elif ground_aqi_row is not None:
        aqi_val, category, data_as_of = float(ground_aqi_row["aqi"]), ground_aqi_row["aqi_category"], ground_aqi_row["timestamp"]
    else:
        aqi_val, category, data_as_of = model_aqi, model_category, pollutant_ts

    is_estimated = False  # both sources here are live; no static fallback left

    if pollutants is None:
        pollutants = {"co": None, "no2": None, "o3": None, "pm10": None, "pm25": None, "so2": None}

    return {
        "city": city,
        "arbitration": arbitration,
        "timestamp": datetime.now().isoformat(),
        "data_as_of": data_as_of.isoformat(),
        "is_estimated": is_estimated,
        "data_origin": "live_measurement",
        "aqi": round(aqi_val, 1),
        "category": category,
        "color": AQI_CATEGORY_COLORS.get(category, "#888888"),
        "pollutants": pollutants,
    }



def _predict_aqi_from_pollutants(city: str, co: float, no2: float, o3: float, pm10: float, pm25: float, so2: float, dt: datetime):
    """Shared nowcast logic: pollutant concentrations -> (aqi, category) using
    the trained regressor + classifier. Used by both /api/nowcast (manual
    entry) and get_current (live sensor feed)."""
    lat, lon = CITY_COORDS[city]
    hour, month, dow = dt.hour, dt.month, dt.weekday()
    is_weekend = 1 if dow >= 5 else 0
    city_code = nowcast_city_encoder.transform([city])[0]

    features = pd.DataFrame([{
        "co": co, "no2": no2, "o3": o3, "pm10": pm10, "pm25": pm25, "so2": so2,
        "hour_sin": np.sin(2 * np.pi * hour / 24), "hour_cos": np.cos(2 * np.pi * hour / 24),
        "month_sin": np.sin(2 * np.pi * month / 12), "month_cos": np.cos(2 * np.pi * month / 12),
        "dow_sin": np.sin(2 * np.pi * dow / 7), "dow_cos": np.cos(2 * np.pi * dow / 7),
        "is_weekend": is_weekend, "city_code": city_code,
        "location_lat": lat, "location_lon": lon, "year": dt.year,
    }])[nowcast_meta["features"]]

    pred_aqi = float(nowcast_regressor.predict(features)[0])
    pred_category = nowcast_classifier.predict(features)[0]
    if isinstance(pred_category, np.ndarray):
        pred_category = pred_category[0]
    return pred_aqi, pred_category


@app.post("/api/nowcast")
def nowcast(req: NowcastRequest):
    if req.city not in CITY_COORDS:
        raise HTTPException(404, f"Unknown city: {req.city}")

    dt = datetime.fromisoformat(req.timestamp) if req.timestamp else datetime.now()
    pred_aqi, pred_category = _predict_aqi_from_pollutants(
        req.city, req.co, req.no2, req.o3, req.pm10, req.pm25, req.so2, dt
    )

    return {
        "city": req.city,
        "aqi": round(pred_aqi, 1),
        "category": pred_category,
        "color": AQI_CATEGORY_COLORS.get(pred_category, "#888888"),
    }


def _forecast_from_stored(city: str, stored: list) -> dict:
    """Build a /api/forecast-style payload from STORED forecast rows.

    These are model predictions generated earlier (model_id stored per row),
    not live measurements -- marked `data_origin="database_stored_forecast"`."""
    forecasts = []
    for s in stored:
        horizon = s.get("horizon_hours")
        pred = s.get("predicted_aqi")
        category = s.get("predicted_category") or (aqi_category_from_value(pred) if pred is not None else None)
        forecasts.append({
            "horizon_hours": horizon,
            "predicted_aqi": pred,
            "predicted_category": category,
            "color": AQI_CATEGORY_COLORS.get(category, "#888888"),
            "actual_aqi": None,
            "target_at": s.get("target_at"),
            "generated_at": s.get("generated_at"),
            "model_id": s.get("model_id"),
        })
    forecasts.sort(key=lambda f: (f["horizon_hours"] if f["horizon_hours"] is not None else 0))
    reference_time = stored[0].get("generated_at") if stored else None
    return {
        "city": city,
        "reference_time": reference_time,
        "current_aqi": None,
        "forecasts": forecasts,
        "model_metrics": forecast_engine.forecast_metrics(),
        "data_maturity": None,
        "data_origin": "database_stored_forecast",
    }


def _with_current_aqi(payload: dict, city: str) -> dict:
    """Fill a forecast payload's ``current_aqi`` from the same source
    ``/api/current`` uses, when the forecast itself has none.

    Stored-forecast payloads (``_forecast_from_stored``) have no live history,
    so their ``current_aqi`` is None even though ``/api/current`` returns a real
    reading (live measurement, or the stored-observation fallback). Copy that in
    rather than shipping null. If ``/api/current`` has nothing either, the field
    stays None -- it is never fabricated."""
    if payload.get("current_aqi") is not None:
        return payload
    try:
        current = get_current(city)
        payload["current_aqi"] = current.get("aqi")
        payload["current_aqi_origin"] = current.get("data_origin")
    except HTTPException:
        pass
    return payload


@app.get("/api/forecast/{city}")
def forecast(city: str, reference_time: Optional[str] = None, persist: bool = True):
    """24/48/72hr forecast, built from the LIVE accumulating history
    (live_history.py) instead of the old frozen Jan-2024 CSV.

    Honesty note: the model needs up to 168 hours (7 days) of hourly lag
    history for full accuracy (aqi_lag_168h, aqi_roll_mean_168h, etc).
    Right after a fresh deployment there won't be 7 days of live data yet --
    `data_maturity` in the response tells you exactly how many hours are
    available right now vs the 168 needed. Missing lag features are left as
    NaN; LightGBM (the trained regressor) handles NaN natively, so you still
    get a prediction during warm-up, just built on less context than it was
    validated with -- don't present it as full-confidence until maturity
    reaches 168h."""
    if city not in CITY_COORDS:
        raise HTTPException(404, f"Unknown city: {city}")

    live_df = live_history.get_live_history()
    city_df = live_df[live_df["location_name"] == city].reset_index(drop=True)

    if city_df.empty:
        # No live history in this container. Serve the most recent STORED
        # forecast rows from Supabase instead of failing, clearly marked.
        stored = db_service.query_latest_forecasts(city)
        if stored:
            return _with_current_aqi(_forecast_from_stored(city, stored), city)
        raise HTTPException(
            503,
            f"No live history yet for {city} and no stored forecast in the "
            f"database. Run aqi_model/run_all.py near this service (or keep a "
            f"container with collectors running) so a forecast can be built "
            f"and persisted."
        )

    payload = forecast_engine.build_forecast_payload(city, city_df, reference_time)
    # Persist generated forecasts to Supabase when available (no-op otherwise,
    # and duplicate-safe via the forecast unique constraint).
    if persist:
        db_service.persist_forecast(city, payload)
    return _with_current_aqi(payload, city)


@app.get("/api/historical/{city}")
def historical(city: str, hours: int = 168):
    """Trend-chart series for the last `hours` hours.

    The live path grows as the collectors keep running. When there is no live
    history in this container we fall back to STORED observations -- but only
    those inside the requested time window, so a multi-year-old imported
    dataset is never stitched to the newest live rows as if it were recent."""
    if city not in CITY_COORDS:
        raise HTTPException(404, f"Unknown city: {city}")

    live_df = live_history.get_live_history()
    city_df = live_df[live_df["location_name"] == city].tail(hours)
    if city_df.empty:
        # Serve stored observations from the requested window only (empty when
        # nothing recent is stored -- the UI shows a warm-up state, not stale data).
        since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))
        stored = db_service.query_recent_readings(city, limit=hours, since=since)
        return [
            {
                "timestamp": s["timestamp"],
                "aqi": round(float(s["aqi"]), 1) if s.get("aqi") is not None else None,
                "category": s["category"],
                "pm25": s["pm25"],
                "pm10": s["pm10"],
                "data_origin": "stored_observation",
            }
            for s in reversed(stored or [])
            if s.get("aqi") is not None  # a trend of AQI can't chart null-AQI rows
        ]
    return [
        {
            "timestamp": row["timestamp"].isoformat(),
            "aqi": round(float(row["aqi"]), 1),
            "category": row["aqi_category"],
            "pm25": round(float(row["pm25"]), 1) if pd.notna(row["pm25"]) else None,
            "pm10": round(float(row["pm10"]), 1) if pd.notna(row["pm10"]) else None,
        }
        for _, row in city_df.iterrows()
    ]



@app.get("/api/geojson/{layer}")
def geojson_layer(layer: str):
    if layer not in GEOJSON_FILES:
        raise HTTPException(404, f"Unknown layer: {layer}. Choose from {list(GEOJSON_FILES.keys())}")
    with open(GEOJSON_FILES[layer]) as f:
        return json.load(f)


@app.get("/api/source-attribution")
def source_attribution_all():
    """Rule-based source attribution (Traffic / Industrial / Construction /
    Waste-Burning / Background) for all monitored points. Requires
    traffic_log.csv and weather_current_log.csv to have at least one
    logged snapshot (run traffic_collector.py / weather_collector.py first)."""
    try:
        results = sa.run_for_all_points()
    except FileNotFoundError:
        # No live traffic/weather CSVs in this container. Fall back to the most
        # recent stored source-attribution snapshot per point. Each entry is
        # marked with data_origin so callers can label it as a stored snapshot.
        stored = db_service.query_latest_source_estimates()
        if stored:
            for s in stored:
                s["data_origin"] = "stored_snapshot"
            return stored
        raise HTTPException(
            503,
            "No live traffic/weather CSVs and no stored source-attribution "
            "snapshots exist yet. Run traffic_collector.py and "
            "weather_collector.py where the backend can read their output "
            "(or once near this service) to populate them."
        )
    # Persist a snapshot (hourly, duplicate-safe) when Supabase is configured.
    db_service.persist_source_attribution(results)
    return results


@app.get("/api/source-attribution/{point}")
def source_attribution_point(point: str):
    results = source_attribution_all()
    for r in results:
        if r["point"].lower() == point.lower():
            return r
    raise HTTPException(404, f"Unknown point: {point}. Choose from {list(sa.CITY_COORDS.keys())}")


def _enforcement_from_stored() -> list:
    """Map stored enforcement_alerts rows into the /api/enforcement item shape.

    Only used when live source attribution is unavailable; each item is
    marked `data_origin="stored_alert"` and keeps the alert's own severity /
    status / created_at so it is never mistaken for a fresh live ranking."""
    stored = db_service.query_alerts(status=None, limit=200) or []
    items = []
    for i, a in enumerate(stored):
        reason = a.get("reason")
        action = a.get("recommended_action")
        items.append({
            "point": a.get("point"),
            "priority_score": a.get("priority_score"),
            "aqi_for_ranking": a.get("aqi_for_ranking"),
            "current_aqi": None,
            "forecast_aqi_24h": None,
            "forecast_is_warming_up": None,
            "dominant_source": None,
            "reasons": [reason] if reason else [],
            "recommended_actions": [action] if action else [],
            "sources": {},
            "severity": a.get("severity"),
            "status": a.get("status"),
            "created_at": a.get("created_at"),
            "stored_alert_id": a.get("id"),
            "data_origin": "stored_alert",
            "rank": i + 1,
        })
    items.sort(key=lambda x: (x.get("aqi_for_ranking") or 0, x.get("priority_score") or 0), reverse=True)
    for i, it in enumerate(items):
        it["rank"] = i + 1
    return items


@app.get("/api/enforcement")
def enforcement_intelligence():
    """Ranks all monitored points by priority for inspection/enforcement,
    combining: current + forecasted AQI severity + dominant pollution
    source + current traffic congestion. Rule-based (transparent, not a
    black box) -- this is exactly the kind of reasoning an official would
    want to see justified, not just a ranked list.

    Ranking uses the WORSE of "AQI right now" and "+24h forecast", not the
    forecast alone. Reason: the forecast model needs up to 168 hours of
    live lag history to be fully trained on this deployment's own data
    (see live_history.py) -- a freshly-started system may not have that
    yet, during which the forecast can swing far from current reality.
    An official needs to act on severe pollution happening RIGHT NOW even
    if a still-warming-up forecast optimistically predicts improvement.
    `forecast_is_warming_up` on each item tells the frontend whether the
    +24h number should be shown with reduced confidence."""
    try:
        attribution_results = source_attribution_all()
    except HTTPException:
        # Source attribution (and thus live ranking) isn't available in this
        # container. Serve stored enforcement alerts instead, mapped to the
        # same item shape so the response type (a ranked list) is preserved.
        return _enforcement_from_stored()

    priorities = []
    for attr in attribution_results:
        point = attr["point"]
        # Use nearest known city for AQI forecast (hotspots reuse their nearest city's AQI series)
        point_lat, point_lon = sa.CITY_COORDS[point]
        city_for_aqi, _ = sa.nearest_known_city(point_lat, point_lon)

        # Current AQI: reuse the SAME source the /api/current and
        # /api/health-advisory endpoints use (live measurement, or the stored
        # observation fallback). Previously this read `fc["current_aqi"]`,
        # which is None for stored forecasts -- so every deployed point showed
        # a null current AQI even though /api/current had a real value.
        current_aqi = None
        try:
            current_aqi = get_current(city_for_aqi).get("aqi")
        except HTTPException:
            current_aqi = None

        # +24h forecast: reuse the forecast payload (live history, or the
        # stored/persisted fallback). `data_maturity` is None for stored
        # forecasts, so we read the 24h value structurally and let
        # `_forecast_status` report warming-up honestly -- the old code did
        # `fc["data_maturity"]["is_warming_up"]`, which raised for stored
        # forecasts and wiped BOTH aqi_24h and current_aqi to None.
        aqi_24h = None
        forecast_is_warming_up = None
        try:
            fc = forecast(city_for_aqi, persist=False)
        except HTTPException:
            fc = None
        if fc:
            aqi_24h = next(
                (f["predicted_aqi"] for f in fc["forecasts"]
                 if f["horizon_hours"] == 24),
                None,
            )
            forecast_is_warming_up = _forecast_status(fc)["forecast_is_warming_up"]
            if current_aqi is None:
                current_aqi = fc.get("current_aqi")

        candidate_aqis = [v for v in (current_aqi, aqi_24h) if v is not None]
        # Rank on the WORSE of "now" and "+24h". If neither is available we
        # keep the point but leave the AQI null and rank it last -- the old
        # code defaulted to 150, which looked like a real (moderate) reading.
        aqi_for_ranking = max(candidate_aqis) if candidate_aqis else None

        sources = attr["sources"]
        dominant_source = max(
            (k for k in sources if k != "background_pct"),
            key=lambda k: sources[k]
        )
        congestion_ratio = attr["raw_signals"]["congestion_ratio"]

        # Priority score: weighted combination, higher = more urgent.
        # AQI severity is only part of the score, so a missing AQI contributes
        # 0 here (and the point is still ranked last by aqi_for_ranking=None).
        severity_component = (aqi_for_ranking or 0.0) / 500.0  # normalize against CPCB practical max
        congestion_component = max(0.0, 1.0 - congestion_ratio)
        source_intensity = max(v for k, v in sources.items() if k != "background_pct") / 100.0

        priority_score = round(100 * (0.7 * severity_component + 0.15 * congestion_component + 0.15 * source_intensity), 1)

        reasons = []
        if congestion_ratio < 0.75:
            reasons.append("Heavy traffic congestion")
        if sources["industrial_pct"] > 15:
            reasons.append("Significant industrial contribution")
        if sources["construction_pct"] > 10:
            reasons.append("Active construction dust")
        if sources["waste_burning_pct"] > 20:
            reasons.append("Waste/stubble burning signal")
        if current_aqi and current_aqi > 200:
            reasons.append(f"AQI currently at {round(current_aqi)} ({aqi_category_from_value(current_aqi)})")
        if aqi_24h and aqi_24h > 200:
            reasons.append(f"AQI forecast to reach {round(aqi_24h)} (Poor+) within 24h")
        if aqi_for_ranking is None:
            reasons.append("No current or forecasted AQI available for this point")
        if not reasons:
            reasons.append("No acute signal -- routine monitoring sufficient")

        actions = []
        if sources["traffic_pct"] > 30:
            actions.append("Deploy traffic police / restrict heavy vehicles")
        if sources["construction_pct"] > 10:
            actions.append("Inspect construction sites for dust-control compliance")
        if sources["industrial_pct"] > 15:
            actions.append("Inspect nearby industrial units for emission compliance")
        if sources["waste_burning_pct"] > 20:
            actions.append("Coordinate with neighboring state agencies on stubble burning")
        if not actions:
            actions.append("Continue routine monitoring")

        priorities.append({
            "point": point,
            "priority_score": priority_score,
            "aqi_for_ranking": round(aqi_for_ranking, 1) if aqi_for_ranking is not None else None,
            "current_aqi": round(current_aqi, 1) if current_aqi is not None else None,
            "forecast_aqi_24h": round(aqi_24h, 1) if aqi_24h is not None else None,
            "forecast_is_warming_up": forecast_is_warming_up,
            "dominant_source": dominant_source.replace("_pct", ""),
            "reasons": reasons,
            "recommended_actions": actions,
            "sources": sources,
        })

    # AQI is the primary ranking factor -- higher AQI always ranks first.
    # priority_score (traffic + source mix) only breaks ties between points
    # at the same AQI level, so a very congested/industrial area can still
    # be ranked above an otherwise-identical AQI area, but never above a
    # genuinely worse-AQI area.
    # Points with no AQI at all sort below any real reading (use -1 sentinel;
    # do not coerce None to 0 -- 0 is a valid "Good" AQI).
    priorities.sort(
        key=lambda x: (x["aqi_for_ranking"] if x["aqi_for_ranking"] is not None else -1,
                       x["priority_score"]),
        reverse=True,
    )
    for i, p in enumerate(priorities):
        p["rank"] = i + 1

    # Persist alerts (hourly, duplicate-safe) when Supabase is configured.
    db_service.persist_enforcement(priorities)

    return priorities


# CPCB-published health advisories per AQI band (National Air Quality Index).
HEALTH_ADVISORY_TABLE = {
    "Good": {
        "general": "Air quality is satisfactory. Enjoy outdoor activities.",
        "sensitive": "No precautions needed.",
    },
    "Satisfactory": {
        "general": "Air quality is acceptable. Minor discomfort possible for unusually sensitive people.",
        "sensitive": "Consider reducing prolonged outdoor exertion if you notice symptoms.",
    },
    "Moderate": {
        "general": "May cause breathing discomfort to people with lung disease, children, and older adults.",
        "sensitive": "People with asthma, lung or heart disease should reduce prolonged outdoor exertion.",
    },
    "Poor": {
        "general": "May cause breathing discomfort to most people on prolonged exposure.",
        "sensitive": "Avoid prolonged outdoor exertion. Wear an N95 mask if you must go outside.",
    },
    "Very Poor": {
        "general": "May cause respiratory illness on prolonged exposure. Avoid outdoor activity.",
        "sensitive": "Avoid all outdoor physical activity. Schools should limit outdoor activities.",
    },
    "Severe": {
        "general": "Affects healthy people and seriously impacts those with existing diseases. Avoid outdoor exposure.",
        "sensitive": "Stay indoors. Use an air purifier if available. Seek medical advice if experiencing symptoms.",
    },
}


def _forecast_status(fc) -> dict:
    """Honest forecast-status flags for the advisory payload.

    ``data_maturity`` is ``None`` when the forecast was served from STORED rows
    (`_forecast_from_stored`) -- i.e. this container has no live collector CSVs.
    We report that explicitly instead of inventing a maturity value or crashing.
    ``fc`` may also be ``None`` when there is no forecast at all (live history
    absent AND nothing persisted): we say so rather than fabricate one.

    Returns ``forecast_is_warming_up`` (kept for backward compatibility; None
    when unknown), ``forecast_status`` and ``forecast_data_origin``.
    """
    if not fc:
        return {
            "forecast_is_warming_up": None,
            "forecast_status": "unavailable",
            "forecast_data_origin": None,
        }
    maturity = fc.get("data_maturity")
    if maturity is None:
        return {
            "forecast_is_warming_up": None,
            "forecast_status": "stored",
            "forecast_data_origin": fc.get("data_origin"),
        }
    warming = maturity.get("is_warming_up")
    return {
        "forecast_is_warming_up": warming,
        "forecast_status": "warming_up" if warming else "live",
        "forecast_data_origin": fc.get("data_origin") or "live_measurement",
    }


def _build_health_advisory_payload(current, fc):
    """Shared by the single-city and all-points endpoints so both stay
    in sync with one source of truth for the advisory text logic.

    ``fc`` may be ``None`` (forecast unavailable) or a stored-forecast payload
    whose ``data_maturity`` is ``None``; both are handled without inventing a
    forecast -- the status fields say exactly what happened."""
    category = current["category"]
    advisory = HEALTH_ADVISORY_TABLE.get(category, HEALTH_ADVISORY_TABLE["Moderate"])

    horizon_advisories = []
    for f in (fc or {}).get("forecasts") or []:
        cat = f.get("predicted_category")
        horizon_advisories.append({
            "horizon_hours": f.get("horizon_hours"),
            "predicted_aqi": f.get("predicted_aqi"),
            "category": cat,
            "advisory": HEALTH_ADVISORY_TABLE.get(cat, HEALTH_ADVISORY_TABLE["Moderate"])["general"],
        })

    return {
        "current_aqi": current["aqi"],
        "current_category": category,
        "general_advisory": advisory["general"],
        "sensitive_groups_advisory": advisory["sensitive"],
        "forecast_advisories": horizon_advisories,
        **_forecast_status(fc),
    }


@app.get("/api/health-advisory/{city}")
def health_advisory(city: str):
    if city not in CITY_COORDS:
        raise HTTPException(404, f"Unknown city: {city}")

    current = get_current(city)
    try:
        fc = forecast(city, persist=False)
    except HTTPException as exc:
        # No forecast available at all (live history absent and nothing
        # persisted). Serve the current-AQI advisory with an explicit
        # "unavailable" status instead of 500/503 -- the current reading is
        # still genuinely useful.
        if exc.status_code != 503:
            raise
        fc = None
    return {"city": city, **_build_health_advisory_payload(current, fc)}


@app.get("/api/health-advisory")
def health_advisory_all():
    """Health advisory for all 21 monitored points (5 trained cities +
    13 DPCC hotspots + 3 landmarks) -- the same point set used by Green
    Cover and Enforcement, so all tabs cover the same footprint.

    The 5 trained cities have live sensor/model readings. The other 16
    points don't have their own trained nowcast model, so AQI/forecast
    is proxied from the nearest trained city -- identical honest-proxy
    pattern already used by /api/estimate, with the same distance/label
    fields included so the frontend can show it transparently rather
    than implying false precision."""
    results = []
    for point, (lat, lon) in sa.CITY_COORDS.items():
        nearest_city, dist_km = sa.nearest_known_city(lat, lon)
        try:
            current = get_current(nearest_city)
        except HTTPException:
            # nearest trained city has no current reading at all -- skip this
            # point rather than serving a stale/fake one
            continue
        try:
            fc = forecast(nearest_city, persist=False)
        except HTTPException:
            # forecast unavailable for this point, but the current-AQI advisory
            # is still useful -- include it with forecast_status="unavailable"
            fc = None

        payload = _build_health_advisory_payload(current, fc)
        results.append({
            "point": point,
            "is_directly_measured": point == nearest_city,
            "nearest_known_city": nearest_city,
            "distance_to_nearest_city_km": round(dist_km, 2),
            **payload,
        })
    return results


# ---------------------------------------------------------------
# Hyperlocal search: geocode + arbitrary-point estimate + what-if
# ---------------------------------------------------------------
NCR_BBOX = "76.698,28.908,77.704,28.099"  # left,top,right,bottom for Nominatim viewbox

import requests as _requests  # local alias to avoid confusion with FastAPI request objects


@app.get("/api/search-location")
def search_location(query: str):
    """Free-text location search restricted to the Delhi NCR bbox, via
    OpenStreetMap Nominatim (no API key needed). Lets the user search ANY
    place, not just the 5 pre-registered cities."""
    try:
        resp = _requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": query, "format": "json", "limit": 8,
                "viewbox": NCR_BBOX, "bounded": 1,
            },
            headers={"User-Agent": "UrbanAirAI-Hackathon-Prototype/1.0"},
            timeout=8,
        )
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        raise HTTPException(503, f"Location search failed: {e}")

    return [
        {"name": r["display_name"], "lat": float(r["lat"]), "lon": float(r["lon"])}
        for r in results
    ]


@app.get("/api/estimate")
def estimate_point(lat: float, lon: float):
    """Hyperlocal estimate for ANY lat/lon in Delhi NCR -- not just the 5
    trained cities. AQI/forecast are proxied from the nearest trained
    city (since we don't have live pollutant sensors everywhere), but
    Source Attribution is computed FRESH for the exact point using the
    static geospatial layers + nearest live traffic/weather -- that part
    is genuinely hyperlocal, not a nearest-neighbor copy."""
    nearest_city, dist_km = sa.nearest_known_city(lat, lon)

    proxy_current = get_current(nearest_city)
    proxy_forecast = forecast(nearest_city, persist=False)

    try:
        traffic_log = pd.read_csv(BASE_DIR / "traffic_log.csv", parse_dates=["timestamp"])
        weather_log = pd.read_csv(BASE_DIR / "weather_current_log.csv", parse_dates=["fetch_timestamp"])
        industrial_zones, construction_zones, road_midpoints = sa.load_static_layers()
        attribution = sa.compute_for_arbitrary_point(
            lat, lon, traffic_log, weather_log, industrial_zones, construction_zones,
            road_midpoints, datetime.now()
        )
    except FileNotFoundError:
        attribution = None

    return {
        "lat": lat, "lon": lon,
        "nearest_known_city": nearest_city,
        "distance_to_nearest_city_km": round(dist_km, 2),
        "proxy_note": f"AQI/forecast values are proxied from {nearest_city} "
                      f"({round(dist_km, 1)} km away) -- no live sensor at this exact point.",
        "aqi": proxy_current["aqi"],
        "category": proxy_current["category"],
        "color": proxy_current["color"],
        "forecast": proxy_forecast["forecasts"],
        "source_attribution": attribution,
    }


class WhatIfRequest(BaseModel):
    point: str  # city or hotspot name from CITY_COORDS
    reduce_traffic_pct: float = 0
    reduce_industrial_pct: float = 0
    reduce_construction_pct: float = 0
    reduce_waste_burning_pct: float = 0


@app.post("/api/whatif")
def whatif(req: WhatIfRequest):
    if req.point not in sa.CITY_COORDS:
        raise HTTPException(404, f"Unknown point: {req.point}")

    attribution = source_attribution_point(req.point)
    point_lat, point_lon = sa.CITY_COORDS[req.point]
    city_for_aqi, _ = sa.nearest_known_city(point_lat, point_lon)
    current = get_current(city_for_aqi)

    reductions = {
        "traffic_pct": req.reduce_traffic_pct,
        "industrial_pct": req.reduce_industrial_pct,
        "construction_pct": req.reduce_construction_pct,
        "waste_burning_pct": req.reduce_waste_burning_pct,
    }
    return sa.compute_whatif(attribution, current["aqi"], reductions)


# ---------------------------------------------------------------
# Alerts / Notifications
# ---------------------------------------------------------------
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _get_alert_context(point):
    if point not in sa.CITY_COORDS:
        raise HTTPException(404, f"Unknown point: {point}")

    point_lat, point_lon = sa.CITY_COORDS[point]
    city_for_aqi, _ = sa.nearest_known_city(point_lat, point_lon)

    current = get_current(city_for_aqi)
    fc = forecast(city_for_aqi)
    attribution = source_attribution_point(point)

    enforcement_list = enforcement_intelligence()
    enforcement_entry = next((e for e in enforcement_list if e["point"] == point), None)

    return current, fc["forecasts"], attribution, enforcement_entry


@app.get("/api/alerts/officials/{point}")
def alert_for_officials(point: str):
    current, forecasts, attribution, enforcement_entry = _get_alert_context(point)
    report = alert_generator.generate_official_report(
        point, current["aqi"], current["category"], forecasts, attribution, enforcement_entry
    )
    return {"point": point, "report_text": report}


@app.get("/api/alerts/public/{point}")
def alert_for_public(point: str):
    current, forecasts, attribution, enforcement_entry = _get_alert_context(point)
    sources = attribution["sources"]
    dominant = max((k for k in sources if k != "background_pct"), key=lambda k: sources[k])
    dominant_label = dominant.replace("_pct", "").replace("_", " ").title()
    message = alert_generator.generate_public_advisory(point, current["aqi"], current["category"], dominant_label)
    return {"point": point, "message": message}


class SendAlertRequest(BaseModel):
    point: str
    alert_type: str  # "official" | "public"
    to_email: str


@app.get("/api/alerts/status")
def alert_send_status():
    return {"smtp_configured": notifications.is_configured()}


@app.post("/api/alerts/send")
def send_alert(req: SendAlertRequest):
    to_email = (req.to_email or "").strip()
    if not _EMAIL_RE.match(to_email):
        raise HTTPException(400, "A valid recipient email address is required.")

    if req.alert_type == "official":
        content = alert_for_officials(req.point)
        subject = f"UrbanAir AI -- Enforcement Alert: {req.point}"
        body = content["report_text"]
    elif req.alert_type == "public":
        content = alert_for_public(req.point)
        subject = f"UrbanAir AI -- Air Quality Advisory: {req.point}"
        body = content["message"]
    else:
        raise HTTPException(400, "alert_type must be 'official' or 'public'")

    try:
        result = notifications.send_email(to_email, subject, body)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except TimeoutError:
        raise HTTPException(503, "SMTP request timed out. Please try again later.")
    except Exception as e:
        msg = str(e)
        if "timeout" in msg.lower():
            raise HTTPException(503, "SMTP request timed out. Please try again later.")
        raise HTTPException(500, f"Failed to send email: {e}")

    return {**result, "subject": subject}


# ---------------------------------------------------------------
# Green Cover Index ("Ek Ped Maa Ke Naam" alignment)
# ---------------------------------------------------------------
@app.get("/api/green-cover")
def green_cover_all():
    return gc.compute_for_all_points(sa.CITY_COORDS)


@app.get("/api/green-cover/{point}")
def green_cover_point(point: str):
    if point not in sa.CITY_COORDS:
        raise HTTPException(404, f"Unknown point: {point}")
    lat, lon = sa.CITY_COORDS[point]
    return gc.compute_green_cover_index(point, lat, lon)