"""
Forecast engine (24/48/72h) — one source of truth for the feature builder,
model artifacts and response payload.

Extracted from ``app/main.py`` for two reasons:

1. The API endpoint and any headless process must produce byte-for-byte the
   same forecast payload, so the numbers persisted to Supabase by the
   GitHub Actions sync job are exactly what ``/api/forecast`` would have
   returned had the live CSVs been present.
2. It must be importable WITHOUT FastAPI / rasterio (``green_cover``), so the
   sync job can build forecasts on a machine that only has the model files.

Honesty note (unchanged from the endpoint): the model needs up to
``live_history.REQUIRED_HOURS_FOR_FULL_FORECAST`` (168) hourly lag points for
full accuracy. Missing lag features are left as NaN — LightGBM handles NaN
natively, so a prediction is always produced, but ``data_maturity`` reports
how much context was actually available.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
MODELS_FORECAST_DIR = BASE_DIR / "models_forecast"

HORIZONS = [24, 48, 72]

AQI_CATEGORY_COLORS = {
    "Good": "#00A65A", "Satisfactory": "#8CC63F", "Moderate": "#FFC107",
    "Poor": "#FF7A00", "Very Poor": "#D9534F", "Severe": "#7B1F1F",
}

_artifacts = None


def _load_artifacts():
    """Lazy-load forecast models + encoder + metadata once per process."""
    global _artifacts
    if _artifacts is None:
        models = {
            h: joblib.load(MODELS_FORECAST_DIR / f"forecast_{h}h.pkl")
            for h in HORIZONS
        }
        encoder = joblib.load(MODELS_FORECAST_DIR / "city_encoder.pkl")
        with open(MODELS_FORECAST_DIR / "metadata.json") as f:
            meta = json.load(f)
        _artifacts = (models, encoder, meta)
    return _artifacts


def forecast_metrics() -> dict:
    return _load_artifacts()[2].get("metrics", {})


def aqi_category_from_value(aqi_val: float) -> str:
    if aqi_val <= 50:
        return "Good"
    elif aqi_val <= 100:
        return "Satisfactory"
    elif aqi_val <= 200:
        return "Moderate"
    elif aqi_val <= 300:
        return "Poor"
    elif aqi_val <= 400:
        return "Very Poor"
    return "Severe"


def build_forecast_features_for_row(city_df: pd.DataFrame, idx: int) -> dict:
    row = city_df.iloc[idx]
    aqi_series = city_df["aqi"]

    def safe_shift(offset):
        j = idx - offset
        return float(aqi_series.iloc[j]) if j >= 0 else np.nan

    shifted = aqi_series.shift(1)
    feats = {
        "aqi_lag_1h": safe_shift(1),
        "aqi_lag_24h": safe_shift(24),
        "aqi_lag_48h": safe_shift(48),
        "aqi_lag_72h": safe_shift(72),
        "aqi_lag_168h": safe_shift(168),
        "aqi_roll_mean_24h": shifted.iloc[max(0, idx - 24):idx].mean() if idx >= 24 else np.nan,
        "aqi_roll_std_24h": shifted.iloc[max(0, idx - 24):idx].std() if idx >= 24 else np.nan,
        "aqi_roll_mean_168h": shifted.iloc[max(0, idx - 168):idx].mean() if idx >= 168 else np.nan,
        "aqi_roll_max_24h": shifted.iloc[max(0, idx - 24):idx].max() if idx >= 24 else np.nan,
        "aqi_trend_6h": (safe_shift(1) - safe_shift(7)) if idx >= 7 else np.nan,
        "pm25_lag_24h": float(city_df["pm25"].iloc[idx - 24]) if idx >= 24 else np.nan,
        "pm25_roll_mean_24h": city_df["pm25"].shift(1).iloc[max(0, idx - 24):idx].mean() if idx >= 24 else np.nan,
        "pm10_lag_24h": float(city_df["pm10"].iloc[idx - 24]) if idx >= 24 else np.nan,
        "pm10_roll_mean_24h": city_df["pm10"].shift(1).iloc[max(0, idx - 24):idx].mean() if idx >= 24 else np.nan,
        "hour_sin": np.sin(2 * np.pi * row["hour"] / 24),
        "hour_cos": np.cos(2 * np.pi * row["hour"] / 24),
        "month_sin": np.sin(2 * np.pi * row["month"] / 12),
        "month_cos": np.cos(2 * np.pi * row["month"] / 12),
        "dow_sin": np.sin(2 * np.pi * row["day_of_week"] / 7),
        "dow_cos": np.cos(2 * np.pi * row["day_of_week"] / 7),
        "is_weekend": row["is_weekend"],
        "location_lat": row["location_lat"],
        "location_lon": row["location_lon"],
        "year": row["year"],
    }
    return feats


def build_forecast_payload(city: str, city_df: pd.DataFrame,
                           reference_time: Optional[str] = None) -> dict:
    """Build the live-forecast payload for one city.

    ``city_df`` must be the live (or collected) hourly history for a single
    city, sorted ascending by timestamp. Mirrors the shape returned by
    ``/api/forecast`` (minus the stored-fallback branch, which is API-only).
    """
    from live_history import REQUIRED_HOURS_FOR_FULL_FORECAST

    models, encoder, meta = _load_artifacts()
    city_df = city_df.reset_index(drop=True)

    if reference_time:
        target_ts = pd.to_datetime(reference_time)
        idx = int((city_df["timestamp"] - target_ts).abs().idxmin())
    else:
        idx = len(city_df) - 1

    hours_available = idx + 1
    data_maturity = {
        "hours_available": hours_available,
        "hours_needed_for_full_accuracy": REQUIRED_HOURS_FOR_FULL_FORECAST,
        "is_warming_up": hours_available < REQUIRED_HOURS_FOR_FULL_FORECAST,
    }

    feats = build_forecast_features_for_row(city_df, idx)
    feats["city_code"] = encoder.transform([city])[0]
    X = pd.DataFrame([feats])[meta["features"]]

    current_aqi = float(city_df.loc[idx, "aqi"])
    reference_ts = city_df.loc[idx, "timestamp"].isoformat()

    predictions = []
    for h in HORIZONS:
        pred = float(models[h].predict(X)[0])
        future_idx = idx + h
        actual = float(city_df.loc[future_idx, "aqi"]) if future_idx < len(city_df) else None
        category = aqi_category_from_value(pred)
        predictions.append({
            "horizon_hours": h,
            "predicted_aqi": round(pred, 1),
            "predicted_category": category,
            "color": AQI_CATEGORY_COLORS.get(category, "#888888"),
            "actual_aqi": round(actual, 1) if actual is not None else None,
        })

    return {
        "city": city,
        "reference_time": reference_ts,
        "current_aqi": round(current_aqi, 1),
        "forecasts": predictions,
        "model_metrics": meta["metrics"],
        "data_maturity": data_maturity,
    }
