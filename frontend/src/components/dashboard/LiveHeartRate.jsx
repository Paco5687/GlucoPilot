import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { base44 } from "@/api/base44Client";
import { useViewingData } from "@/hooks/useViewingData";
import { Button } from "@/components/ui/button";
import { Heart, RefreshCw, Loader2 } from "lucide-react";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, ReferenceLine } from "recharts";

const WINDOW_MIN = 90;          // scrolling window width
const POLL_MS = 60 * 1000;      // refetch cadence (backend syncs every ~2 min)
const STALE_MIN = 20;           // older than this → flag as stale

export default function LiveHeartRate() {
  const { fetchEntity, isViewingShared } = useViewingData();
  const [points, setPoints] = useState([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [now, setNow] = useState(Date.now());
  const seeded = useRef(false);

  const fetchPoints = useCallback(async () => {
    const since = new Date(Date.now() - WINDOW_MIN * 60000).toISOString();
    const rows = await fetchEntity("FitbitHeartRate", "-timestamp", 400, { timestamp: { $gte: since } });
    setPoints(
      rows
        .map((r) => ({ t: new Date(r.timestamp).getTime(), bpm: r.bpm }))
        .filter((p) => !Number.isNaN(p.t) && p.bpm != null)
        .sort((a, b) => a.t - b.t)
    );
    setNow(Date.now());
    setLoading(false);
  }, [fetchEntity]);

  // Seed a fresh pull on first mount (owner only), then poll the entity.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!isViewingShared && !seeded.current) {
        seeded.current = true;
        try { await base44.functions.invoke("googleHealth", { action: "sync_hr", minutes: WINDOW_MIN }); } catch { /* scheduler covers it */ }
      }
      if (!cancelled) await fetchPoints();
    })();
    const poll = setInterval(fetchPoints, POLL_MS);
    return () => { cancelled = true; clearInterval(poll); };
  }, [fetchPoints, isViewingShared]);

  async function handleSync() {
    setSyncing(true);
    try {
      await base44.functions.invoke("googleHealth", { action: "sync_hr", minutes: WINDOW_MIN });
      await fetchPoints();
    } finally {
      setSyncing(false);
    }
  }

  const stats = useMemo(() => {
    if (!points.length) return null;
    const bpms = points.map((p) => p.bpm);
    const latest = points[points.length - 1];
    return {
      current: latest.bpm,
      ageMin: Math.round((now - latest.t) / 60000),
      min: Math.min(...bpms),
      max: Math.max(...bpms),
      avg: Math.round(bpms.reduce((a, b) => a + b, 0) / bpms.length),
    };
  }, [points, now]);

  const domain = [now - WINDOW_MIN * 60000, now];
  const fmtTime = (t) => new Date(t).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  const stale = stats && stats.ageMin > STALE_MIN;

  return (
    <div className="bg-card rounded-xl border border-border p-5">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Heart className={`w-4 h-4 text-rose-500 ${stats && !stale ? "animate-pulse" : ""}`} />
          <h2 className="text-sm font-semibold">Live Heart Rate</h2>
          <span className="text-[10px] text-muted-foreground px-1.5 py-0.5 rounded bg-muted">Fitbit</span>
        </div>
        {!isViewingShared && (
          <Button variant="outline" size="sm" onClick={handleSync} disabled={syncing} className="gap-1.5 text-xs">
            {syncing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
            {syncing ? "Syncing..." : "Refresh"}
          </Button>
        )}
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-40"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>
      ) : !stats ? (
        <div className="h-40 flex flex-col items-center justify-center text-center gap-2">
          <p className="text-sm text-muted-foreground">No recent heart-rate data.</p>
          <p className="text-xs text-muted-foreground">Fitbit syncs to the cloud every ~5–15 min; make sure the watch is near a phone.</p>
        </div>
      ) : (
        <>
          <div className="flex items-end gap-4 mb-2">
            <div className="flex items-baseline gap-1">
              <span className="text-4xl font-bold tabular-nums text-rose-500">{stats.current}</span>
              <span className="text-sm text-muted-foreground">bpm</span>
            </div>
            <div className="text-xs text-muted-foreground pb-1">
              <span className={stale ? "text-amber-500" : ""}>
                {stats.ageMin <= 1 ? "just now" : `${stats.ageMin} min ago`}{stale ? " · stale" : ""}
              </span>
              <span className="mx-1.5">·</span>
              {WINDOW_MIN}-min range {stats.min}–{stats.max} · avg {stats.avg}
            </div>
          </div>
          <div className="h-44">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: -14 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
                <XAxis
                  type="number"
                  dataKey="t"
                  domain={domain}
                  scale="time"
                  tickFormatter={fmtTime}
                  tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                  minTickGap={50}
                />
                <YAxis
                  tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                  domain={[(min) => Math.max(30, Math.floor((min - 5) / 5) * 5), (max) => Math.ceil((max + 5) / 5) * 5]}
                  width={34}
                />
                <Tooltip
                  contentStyle={{ backgroundColor: "hsl(var(--card))", border: "1px solid hsl(var(--border))", borderRadius: "8px", fontSize: "12px" }}
                  labelFormatter={(t) => fmtTime(t)}
                  formatter={(v) => [`${v} bpm`, "Heart rate"]}
                />
                {stats && <ReferenceLine y={stats.avg} stroke="hsl(var(--muted-foreground))" strokeDasharray="2 4" strokeOpacity={0.5} />}
                <Line type="monotone" dataKey="bpm" stroke="#f43f5e" strokeWidth={2} dot={false} isAnimationActive={false} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
    </div>
  );
}
