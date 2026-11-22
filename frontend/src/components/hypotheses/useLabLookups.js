import { useCallback, useEffect, useState } from "react";

// Stored lab lookups for the open hypotheses (GET /api/hypothesis-labs): which
// have results waiting and how many labs are not yet in their evidence. While
// the background queue runs, it reads again every 15 seconds.

const POLL_MS = 15000;
const EMPTY = { running: false, ready: {}, pending: [] };

export default function useLabLookups(enabled) {
  const [status, setStatus] = useState(EMPTY);

  const load = useCallback(async (path = "/api/hypothesis-labs", options = undefined) => {
    try {
      const response = await fetch(path, { credentials: "same-origin", ...options });
      if (!response.ok) return;
      const data = await response.json();
      setStatus({ running: Boolean(data?.running), ready: data?.ready || {}, pending: data?.pending || [] });
    } catch {
      // The Find labs buttons work without the counts.
    }
  }, []);

  useEffect(() => {
    if (enabled) load();
  }, [enabled, load]);

  useEffect(() => {
    if (!enabled || !status.running) return undefined;
    const timer = setTimeout(() => load(), POLL_MS);
    return () => clearTimeout(timer);
  }, [enabled, status, load]);

  const refresh = useCallback(() => load(), [load]);
  const queue = useCallback(() => load("/api/hypothesis-labs/queue", { method: "POST" }), [load]);

  return { ...status, refresh, queue };
}
