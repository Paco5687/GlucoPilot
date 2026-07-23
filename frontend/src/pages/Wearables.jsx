import { useState, useEffect, useCallback, useMemo } from "react";
import { base44 } from "@/api/base44Client";
import { useViewingData } from "@/hooks/useViewingData";
import { Button } from "@/components/ui/button";
import { Loader2, RefreshCw, Heart, Moon, Droplets, Wind, Footprints, Flame, Activity } from "lucide-react";
import { toast } from "sonner";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, Legend } from "recharts";
import LiveHeartRate from "../components/dashboard/LiveHeartRate";
import GlucoseHeartRateOverlay from "../components/dashboard/GlucoseHeartRateOverlay";

const RANGES = [
  { key: 30, label: "30d" },
  { key: 90, label: "90d" },
  { key: 365, label: "1y" },
];

// One series per chart; thin marks, recessive grid, direct tooltip.
const METRICS = [
  { key: "resting_heart_rate", label: "Resting Heart Rate", unit: "bpm", color: "#f43f5e", icon: Heart, round: 0 },
  // Both rings measure HRV overnight on the same unit but different baselines,
  // so they share one axis as two named series rather than being averaged.
  {
    key: "hrv",
    label: "Heart Rate Variability",
    unit: "ms",
    color: "#8b5cf6",
    icon: Activity,
    round: 1,
    seriesLabel: "Google Health",
    compare: { key: "hrv_oura", label: "Oura", color: "#0ea5e9" },
  },
  { key: "sleep_hours", label: "Sleep", unit: "h", color: "#6366f1", icon: Moon, round: 1 },
  { key: "spo2_avg", label: "SpO₂ (overnight avg)", unit: "%", color: "#0ea5e9", icon: Droplets, round: 1, domain: [90, 100] },
  { key: "breathing_rate", label: "Respiratory Rate", unit: "br/min", color: "#14b8a6", icon: Wind, round: 1 },
  { key: "steps", label: "Steps", unit: "", color: "#f97316", icon: Footprints, round: 0 },
  { key: "active_minutes", label: "Active Minutes", unit: "min", color: "#10b981", icon: Flame, round: 0 },
];

function fmt(n, round) {
  if (n == null) return "—";
  return round ? Number(n).toFixed(round) : Math.round(n).toLocaleString();
}

function MetricCard({ metric, rows }) {
  const compare = metric.compare;
  const series = useMemo(
    () =>
      rows
        .filter((d) => d[metric.key] != null || (compare && d[compare.key] != null))
        .map((d) => ({
          date: d.date,
          label: new Date(d.date + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" }),
          value: d[metric.key] ?? null,
          compare: compare ? d[compare.key] ?? null : null,
        })),
    [rows, metric.key, compare]
  );

  if (series.length === 0) return null;
  // Headline number stays on the primary series so it means one thing.
  const values = series.map((s) => s.value).filter((v) => v != null);
  const latest = values[values.length - 1];
  const avg = values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
  const Icon = metric.icon;

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Icon className="w-4 h-4" style={{ color: metric.color }} />
          <h3 className="text-sm font-semibold">{metric.label}</h3>
        </div>
        <div className="text-right">
          <div className="text-lg font-semibold tabular-nums" style={{ color: metric.color }}>
            {fmt(latest, metric.round)}<span className="text-xs text-muted-foreground ml-0.5">{metric.unit}</span>
          </div>
          <div className="text-[10px] text-muted-foreground">avg {fmt(avg, metric.round)}</div>
        </div>
      </div>
      <div className="h-40">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={series} margin={{ top: 4, right: 8, bottom: 0, left: -12 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
            <XAxis dataKey="label" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} interval="preserveStartEnd" minTickGap={40} />
            <YAxis
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              domain={metric.domain || ["auto", "auto"]}
              width={40}
              allowDecimals={metric.round > 0}
            />
            <Tooltip
              contentStyle={{ backgroundColor: "hsl(var(--card))", border: "1px solid hsl(var(--border))", borderRadius: "8px", fontSize: "12px" }}
              formatter={(v, name) => [`${fmt(v, metric.round)} ${metric.unit}`.trim(), name]}
            />
            {compare && (
              <Legend
                verticalAlign="top"
                height={20}
                iconType="plainline"
                iconSize={10}
                wrapperStyle={{ fontSize: "10px", color: "hsl(var(--muted-foreground))" }}
              />
            )}
            <Line
              type="monotone"
              dataKey="value"
              stroke={metric.color}
              strokeWidth={2}
              dot={false}
              connectNulls
              name={metric.seriesLabel || metric.label}
            />
            {compare && (
              <Line
                type="monotone"
                dataKey="compare"
                stroke={compare.color}
                strokeWidth={2}
                dot={false}
                connectNulls
                name={compare.label}
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export default function Wearables() {
  const { fetchEntity, isViewingShared } = useViewingData();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [range, setRange] = useState(90);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    const [google, oura] = await Promise.all([
      fetchEntity("FitbitDaily", "-date", 400, { source: "google_health" }),
      fetchEntity("OuraDaily", "-date", 400),
    ]);
    // Oura's nightly HRV joins on date under its own key — deliberately a
    // second series rather than a fallback, since the two rings read different
    // baselines on the same night. Nothing else is taken from Oura here, so no
    // other metric can be silently swapped between devices.
    const byDate = new Map(google.map((d) => [d.date, { ...d }]));
    for (const d of oura) {
      if (!d.date || d.hrv == null) continue;
      byDate.set(d.date, { ...(byDate.get(d.date) || { date: d.date }), hrv_oura: d.hrv });
    }
    setRows([...byDate.values()]);
    setLoading(false);
  }, [fetchEntity]);

  useEffect(() => {
    load();
  }, [load]);

  // chronological rows within the selected window, with derived sleep hours
  const windowed = useMemo(() => {
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - range);
    const from = cutoff.toISOString().slice(0, 10);
    return [...rows]
      .filter((d) => d.date >= from)
      .sort((a, b) => a.date.localeCompare(b.date))
      .map((d) => ({ ...d, sleep_hours: d.sleep_minutes != null ? d.sleep_minutes / 60 : null }));
  }, [rows, range]);

  async function handleSync(days) {
    setBusy(true);
    try {
      const res = await base44.functions.invoke("googleHealth", { action: "sync", days });
      toast.success(`Synced ${res.data.days_synced} days (${res.data.created} new)`);
      await load();
    } catch (err) {
      toast.error(err?.response?.data?.error || err.message || "Sync failed");
    }
    setBusy(false);
  }

  async function handleHrBackfill() {
    setBusy(true);
    toast.info("Backfilling 7 days of minute-level heart rate — this can take a minute…");
    try {
      const res = await base44.functions.invoke("googleHealth", { action: "sync_hr", minutes: 7 * 1440 });
      toast.success(`Heart rate: ${res.data.created} new minute-readings`);
    } catch (err) {
      toast.error(err?.response?.data?.error || err.message || "HR backfill failed");
    }
    setBusy(false);
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-lg font-semibold">Wearables</h1>
          <p className="text-xs text-muted-foreground">Fitbit daily metrics via the Google Health API, with Oura HRV alongside</p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex rounded-lg border border-border overflow-hidden">
            {RANGES.map((r) => (
              <button
                key={r.key}
                onClick={() => setRange(r.key)}
                className={`text-xs px-3 py-1.5 ${range === r.key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}
              >
                {r.label}
              </button>
            ))}
          </div>
          {!isViewingShared && (
            <>
              <Button variant="outline" size="sm" onClick={() => handleSync(30)} disabled={busy} className="gap-1.5 text-xs">
                {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />} Sync
              </Button>
              <Button variant="outline" size="sm" onClick={() => handleSync(365)} disabled={busy} className="gap-1.5 text-xs">
                Backfill 1y
              </Button>
              <Button variant="outline" size="sm" onClick={handleHrBackfill} disabled={busy} className="gap-1.5 text-xs">
                Backfill HR 7d
              </Button>
            </>
          )}
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-40">
          <Loader2 className="w-5 h-5 animate-spin text-primary" />
        </div>
      ) : rows.length === 0 ? (
        <div className="bg-card rounded-xl border border-border p-8 text-center">
          <p className="text-sm text-muted-foreground mb-3">
            No Google Health data yet. Connect on the Connections page, then sync.
          </p>
          {!isViewingShared && (
            <Button size="sm" onClick={() => handleSync(30)} disabled={busy}>
              {busy ? "Syncing..." : "Sync now"}
            </Button>
          )}
        </div>
      ) : (
        <div className="space-y-4">
          <LiveHeartRate />
          <GlucoseHeartRateOverlay />
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {METRICS.map((m) => (
              <MetricCard key={m.key} metric={m} rows={windowed} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
