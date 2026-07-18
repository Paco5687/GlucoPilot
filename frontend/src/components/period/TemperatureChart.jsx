import { useMemo } from "react";
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, ReferenceArea, ReferenceLine, CartesianGrid,
} from "recharts";

// Soft phase band fills; identity is also carried by the legend chips below
// (never color alone).
const PHASE_FILLS = {
  menstrual: { fill: "#ef4444", opacity: 0.12, label: "Menstrual" },
  follicular: { fill: "#0ea5e9", opacity: 0.1, label: "Follicular" },
  ovulation: { fill: "#f59e0b", opacity: 0.22, label: "Ovulation" },
  luteal: { fill: "#8b5cf6", opacity: 0.1, label: "Luteal" },
};

function TempTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="bg-card border border-border rounded-lg px-3 py-2 text-xs shadow-lg">
      <p className="font-medium">{new Date(d.date + "T00:00:00").toLocaleDateString([], { month: "short", day: "numeric" })}</p>
      <p>Temp deviation: <b>{d.temp > 0 ? "+" : ""}{d.temp?.toFixed(2)} °C</b></p>
      {d.phase && <p className="capitalize text-muted-foreground">{d.phase} phase</p>}
    </div>
  );
}

export default function TemperatureChart({ ouraDays, logs }) {
  const { data, bands } = useMemo(() => {
    const phaseByDate = new Map(logs.filter((l) => l.phase).map((l) => [l.date, l.phase]));
    const rows = (ouraDays || [])
      .filter((d) => d.date && d.readiness_temperature_deviation != null)
      .map((d) => ({
        date: d.date,
        temp: d.readiness_temperature_deviation,
        phase: phaseByDate.get(d.date) || null,
      }))
      .sort((a, b) => a.date.localeCompare(b.date));

    // contiguous same-phase runs → one band each
    const runs = [];
    let current = null;
    for (const row of rows) {
      if (row.phase && current && current.phase === row.phase) {
        current.end = row.date;
      } else {
        if (current) runs.push(current);
        current = row.phase ? { phase: row.phase, start: row.date, end: row.date } : null;
      }
    }
    if (current) runs.push(current);
    return { data: rows, bands: runs };
  }, [ouraDays, logs]);

  if (data.length < 14) {
    return (
      <div className="bg-card rounded-xl border border-border p-4">
        <h3 className="text-sm font-semibold mb-2">Nightly temperature</h3>
        <p className="text-xs text-muted-foreground py-6 text-center">
          Not enough Oura temperature data yet — the phase inference needs about a month of nights.
        </p>
      </div>
    );
  }

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center justify-between mb-1 flex-wrap gap-2">
        <h3 className="text-sm font-semibold">Nightly temperature — the signal behind the phases</h3>
        <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
          {Object.entries(PHASE_FILLS).map(([key, s]) => (
            <span key={key} className="flex items-center gap-1">
              <i className="inline-block w-3 h-3 rounded-sm" style={{ background: s.fill, opacity: Math.min(1, s.opacity * 4) }} />
              {s.label}
            </span>
          ))}
        </div>
      </div>
      <p className="text-xs text-muted-foreground mb-2">
        Deviation from her personal baseline. The sustained rises are the post-ovulation (luteal) shifts the inference detects.
      </p>
      <div className="h-56">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
            {bands.map((b, i) => {
              const s = PHASE_FILLS[b.phase];
              if (!s) return null;
              return <ReferenceArea key={i} x1={b.start} x2={b.end} fill={s.fill} fillOpacity={s.opacity} stroke="none" />;
            })}
            <XAxis
              dataKey="date"
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              tickFormatter={(d) => new Date(d + "T00:00:00").toLocaleDateString([], { month: "short", day: "numeric" })}
              minTickGap={40}
              tickLine={false}
            />
            <YAxis
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              tickFormatter={(v) => `${v > 0 ? "+" : ""}${v.toFixed(1)}°`}
              width={40}
              tickLine={false}
            />
            <Tooltip content={<TempTooltip />} />
            <ReferenceLine y={0} stroke="hsl(var(--muted-foreground))" strokeOpacity={0.4} />
            <Line type="monotone" dataKey="temp" stroke="hsl(var(--primary))" strokeWidth={2} dot={false} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
