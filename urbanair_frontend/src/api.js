// Configurable API base.
//   Dev  : leave VITE_API_BASE_URL unset -> "/api" (Vite dev-server proxy).
//   Prod : set VITE_API_BASE_URL=https://<render-backend>.onrender.com/api
// Only public, non-secret values belong in VITE_* vars (they ship in the
// browser bundle) — never put DB credentials or service keys here.
const RAW_BASE = import.meta.env.VITE_API_BASE_URL || "/api";
const BASE = RAW_BASE.replace(/\/+$/, "");

async function request(path, options = {}) {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? 20000;
  const timeoutId = setTimeout(() => controller.abort("Request timed out"), timeoutMs);

  try {
    const res = await fetch(`${BASE}${path}`, {
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      ...options,
    });
    if (!res.ok) {
      let detail = `API ${path} failed: ${res.status}`;
      try {
        const body = await res.json();
        if (body && body.detail) {
          detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
        }
      } catch {
      }
      const err = new Error(detail);
      err.status = res.status;
      throw err;
    }
    return res.json();
  } catch (err) {
    if (controller.signal.aborted) {
      throw new Error("Request timed out. Please try again.");
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
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
  alertsStatus: () => request("/alerts/status"),
  alertOfficials: (point) => request(`/alerts/officials/${encodeURIComponent(point)}`),
  alertPublic: (point) => request(`/alerts/public/${encodeURIComponent(point)}`),
  sendAlert: (payload) =>
    // Backend SMTP timeout is 10s; content generation + pool queries add a
    // little on top, so allow 30s total before aborting (still bounded).
    request("/alerts/send", { method: "POST", body: JSON.stringify(payload), timeoutMs: 30000 }),
};