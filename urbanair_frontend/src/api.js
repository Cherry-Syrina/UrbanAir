// Configurable API base.
//   Dev  : leave VITE_API_BASE_URL unset -> "/api" (Vite dev-server proxy).
//   Prod : set VITE_API_BASE_URL=https://<render-backend>.onrender.com/api
// Only public, non-secret values belong in VITE_* vars (they ship in the
// browser bundle) — never put DB credentials or service keys here.
const RAW_BASE = import.meta.env.VITE_API_BASE_URL || "/api";
const BASE = RAW_BASE.replace(/\/+$/, "");

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    throw new Error(`API ${path} failed: ${res.status}`);
  }
  return res.json();
}

export const api = {
  cities: () => request("/cities"),
  current: (city) => request(`/current/${encodeURIComponent(city)}`),
  forecast: (city, referenceTime) =>
    request(
      `/forecast/${encodeURIComponent(city)}${referenceTime ? `?reference_time=${encodeURIComponent(referenceTime)}` : ""}`
    ),
  historical: (city, hours = 168) => request(`/historical/${encodeURIComponent(city)}?hours=${hours}`),
  geojson: (layer) => request(`/geojson/${layer}`),
  nowcast: (payload) =>
    request("/nowcast", { method: "POST", body: JSON.stringify(payload) }),
  sourceAttribution: (point) => request(`/source-attribution/${encodeURIComponent(point)}`),
  sourceAttributionAll: () => request("/source-attribution"),
  enforcement: () => request("/enforcement"),
  healthAdvisory: (city) => request(`/health-advisory/${encodeURIComponent(city)}`),
  healthAdvisoryAll: () => request("/health-advisory"),
  searchLocation: (query) => request(`/search-location?query=${encodeURIComponent(query)}`),
  estimate: (lat, lon) => request(`/estimate?lat=${lat}&lon=${lon}`),
  whatif: (payload) => request("/whatif", { method: "POST", body: JSON.stringify(payload) }),
  greenCoverAll: () => request("/green-cover"),
  greenCoverPoint: (point) => request(`/green-cover/${encodeURIComponent(point)}`),
  dbHealth: () => request("/db/health"),
  aqiHistory: (city, hours = 168, limit = 200, offset = 0) =>
    request(
      `/aqi/history/${encodeURIComponent(city)}?hours=${hours}&limit=${limit}&offset=${offset}`
    ),
};