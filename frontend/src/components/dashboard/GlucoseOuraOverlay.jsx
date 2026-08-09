import { useMemo, useState } from "react";
import {
  ResponsiveContainer, ComposedChart, Line, Bar, XAxis, YAxis,
  Tooltip, CartesianGrid, ReferenceLine,
} from "recharts";

// This is a day-scale correlation chart: one point per calendar day, matched
// against Oura's nightly scores. It deliberately does NOT follow the
// dashboard's hour-scale range picker — a 3h glucose window would produce a
// single "day" and an invisible one-point line, which is exactly the broken
// state this replaced. It owns its own window instead.

const WINDOWS = [
  { key: 14, label: "14d" },
  { key: 30, label: "30d" },
  { key: 60, label: "60d" },
];

// Fewer readings than this and a "daily" average is really a fragment of a
// day; the day still shows its Oura scores, just no glucose stats.
const MIN_READINGS_PER_DAY = 24;

const OURA_METRICS = [
  { key: "sleep_score", label: "Sleep Score", color: "#6366f1" },
  { key: "readiness_score", label: "Readiness", color: "#10b981" },
  { key: "activity_score", label: "Activity", color: "#f97316" },
];

const GLUCOSE_METRICS = [
  { key: "avg_glucose", label: "Avg Glucose", color: "#0d9668" },
  { key: "tir", label: "Time in Range %", color: "#22c55e" },
];

// Local calendar day — Oura's `date` is the local sleep day, so glucose must
// bucket the same way. toISOString() would use UTC and push every evening
// reading onto the next day's bucket.
export function localDayKey(timestamp) {
  const d = new Date(timestamp);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function OverlayTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="bg-card border border-border rounded-lg p-3 shadow-lg text-xs space-y-1">
      <p className="font-medium mb-1.5">{label}</p>
      {payload.map((p, i) => (
        <div key={i} className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full" style={{ backgroundColor: p.color }} />
          <span className="text-muted-foreground">{p.name}:</span>
          <span className="font-medium">
            {p.value != null ? (p.name.includes("Glucose") ? `${p.value} mg/dL` : p.name.includes("Range") ? `${p.value}%` : p.value) : "—"}
          </span>
        </div>
      ))}
    </div>
  );
}

export default function GlucoseOuraOverlay({ readings, ouraData }) {
  const [windowDays, setWindowDays] = useState(14);
  const [activeOura, setActiveOura] = useState(["sleep_score"]);
  const [activeGlucose, setActiveGlucose] = useState(["avg_glucose"]);

  const chartData = useMemo(() => {
    if (!readings?.length || !ouraData?.length) return [];

    const today = localDayKey(Date.now());
    const cutoff = localDayKey(Date.now() - windowDays * 86400000);

    const glucoseByDay = {};
    readings.forEach((r) => {
      const day = localDayKey(r.timestamp);
      if (!glucoseByDay[day]) glucoseByDay[day] = [];
      glucoseByDay[day].push(r.value);
    });

    const ouraByDay = {};
    ouraData.forEach((d) => { ouraByDay[d.date] = d; });

    const allDays = new Set([...Object.keys(glucoseByDay), ...Object.keys(ouraByDay)]);
    return [...allDays]
      .filter((day) => day >= cutoff && day < today) // today is still accumulating
      .sort()
      .map((day) => {
        const gVals = glucoseByDay[day] || [];
        const oura = ouraByDay[day] || {};
        const enough = gVals.length >= MIN_READINGS_PER_DAY;
        const avg = enough ? Math.round(gVals.reduce((s, v) => s + v, 0) / gVals.length) : null;
        const tir = enough
          ? Math.round((gVals.filter((v) => v >= 70 && v <= 180).length / gVals.length) * 100)
          : null;
        return {
          date: day,
          label: new Date(day + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" }),
          avg_glucose: avg,
          tir,
          sleep_score: oura.sleep_score,
          readiness_score: oura.readiness_score,
          activity_score: oura.activity_score,
        };
      });
  }, [readings, ouraData, windowDays]);

  if (!chartData.length) return null;

  const toggleOura = (key) => setActiveOura((p) => p.includes(key) ? p.filter((k) => k !== key) : [...p, key]);
  const toggleGlucose = (key) => setActiveGlucose((p) => p.includes(key) ? p.filter((k) => k !== key) : [...p, key]);

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-3">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold">Glucose × Oura by day</h3>
          <div className="flex rounded-lg border border-border overflow-hidden">
            {WINDOWS.map((w) => (
              <button
                key={w.key}
                onClick={() => setWindowDays(w.key)}
                className={`text-[10px] px-2 py-0.5 ${windowDays === w.key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}
              >
                {w.label}
              </button>
            ))}
          </div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {GLUCOSE_METRICS.map((m) => (
            <button
              key={m.key}
              onClick={() => toggleGlucose(m.key)}
              className={`text-[10px] px-2 py-1 rounded-full font-medium transition-colors border ${
                activeGlucose.includes(m.key) ? "bg-primary/10 text-primary border-primary/30" : "bg-muted text-muted-foreground border-transparent"
              }`}
            >
              {m.label}
            </button>
          ))}
          <span className="text-muted-foreground text-[10px] px-1">|</span>
          {OURA_METRICS.map((m) => (
            <button
              key={m.key}
              onClick={() => toggleOura(m.key)}
              className={`text-[10px] px-2 py-1 rounded-full font-medium transition-colors ${
                activeOura.includes(m.key) ? "text-white" : "bg-muted text-muted-foreground"
              }`}
              style={activeOura.includes(m.key) ? { backgroundColor: m.color } : {}}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>

      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis
              dataKey="label"
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              interval="preserveStartEnd"
            />
            {/* Left Y axis — glucose / TIR */}
            <YAxis
              yAxisId="glucose"
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              domain={["auto", "auto"]}
              label={{ value: "mg/dL", angle: -90, position: "insideLeft", style: { fontSize: 10, fill: "hsl(var(--muted-foreground))" } }}
            />
            {/* Right Y axis — Oura scores 0–100 */}
            <YAxis
              yAxisId="oura"
              orientation="right"
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              domain={[0, 100]}
              label={{ value: "Score", angle: 90, position: "insideRight", style: { fontSize: 10, fill: "hsl(var(--muted-foreground))" } }}
            />
            <Tooltip content={<OverlayTooltip />} />
            <ReferenceLine yAxisId="glucose" y={180} stroke="hsl(var(--glucose-high))" strokeDasharray="4 4" strokeOpacity={0.5} />
            <ReferenceLine yAxisId="glucose" y={70} stroke="hsl(var(--glucose-low))" strokeDasharray="4 4" strokeOpacity={0.5} />

            {GLUCOSE_METRICS.filter((m) => activeGlucose.includes(m.key)).map((m) => (
              m.key === "tir" ? (
                <Bar key={m.key} yAxisId="oura" dataKey={m.key} fill={m.color} name={m.label} opacity={0.25} barSize={12} />
              ) : (
                // Small dots so a day isolated by gaps still renders.
                <Line key={m.key} yAxisId="glucose" type="monotone" dataKey={m.key} stroke={m.color} strokeWidth={2.5} dot={{ r: 2 }} name={m.label} connectNulls />
              )
            ))}

            {OURA_METRICS.filter((m) => activeOura.includes(m.key)).map((m) => (
              <Line key={m.key} yAxisId="oura" type="monotone" dataKey={m.key} stroke={m.color} strokeWidth={2} dot={{ r: 2 }} name={m.label} strokeDasharray="6 3" connectNulls />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <p className="text-[10px] text-muted-foreground mt-2 text-center">
        One point per day (today excluded while incomplete) · Solid line = glucose, left axis · Dashed lines = Oura scores, right axis · Bars = Time in Range %
      </p>
    </div>
  );
}
