import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Fetch a resource with settled loading/error state and request de-duplication.
 *
 * Why this exists: the app refreshes every 60s. The all-points endpoints used to
 * take ~85s, so a new request was fired while the previous one was still in
 * flight and the old code's `.catch(console.error)` never moved the UI out of
 * "Loading...". This hook:
 *   - never stacks requests for the same resource (an in-flight load is a no-op);
 *   - always settles `loading` (finally), even on error/timeout;
 *   - surfaces `error` so the component can render a real error state instead of
 *     an infinite spinner;
 *   - ignores results that arrive after unmount.
 */
export function useResource(fetcher, { enabled = true } = {}) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(enabled);
  const inflight = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const reload = useCallback(
    async ({ silent = false } = {}) => {
      if (inflight.current) return null; // de-dupe: don't stack requests
      inflight.current = true;
      if (!silent) setLoading(true);
      try {
        const result = await fetcher();
        if (mounted.current) {
          setData(result);
          setError(null);
        }
        return result;
      } catch (e) {
        if (mounted.current) setError(e?.message || String(e));
        return null;
      } finally {
        inflight.current = false;
        if (mounted.current) setLoading(false);
      }
    },
    [fetcher]
  );

  useEffect(() => {
    if (enabled) reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, reload]);

  return { data, error, loading, reload };
}
