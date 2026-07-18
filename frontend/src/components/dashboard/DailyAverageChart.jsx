import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine, ReferenceArea } from "recharts";
import { format } from "date-fns";

function AvgTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const d = payload[0]?.payload;
  if (!d) return null;
  return (
    <div className="bg-card border border-border rounded-lg shadow-lg p-2.5 text-xs">
      <p className="font-medium mb-1">{d.label}</p>
      <p className="font-mono">Avg: <strong>{d.avg}</strong> mg/dL</p>
      <p className="text-muted-foreground">Range: {d.min}–{d.max} mg/dL</p>
      <p className="text-muted-foreground">SD: ±{d.sd}</p>
    </div>
  );
}

export default function DailyAverageChart({ readings }) {
  if (!readings?.length) return null;

  const byDay = {};
  readings.forEach((r) => {
    const dayKey = format(new Date(r.timestamp), "yyyy-MM-dd");
    if (!byDay[dayKey]) byDay[dayKey] = [];
    byDay[dayKey].push(r.value);
  });

  const data = Object.entries(byDay)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, values]) => {
      const avg = Math.round(values.reduce((s, v) => s + v, 0) / values.length);
      const variance = values.reduce((s, v) => s + Math.pow(v - avg, 2), 0) / values.length;
      return {
        day,
        label: format(new Date(day), "MMM d"),
        avg,
        min: Math.min(...values),
        max: Math.max(...values),
        sd: Math.round(Math.sqrt(variance)),
      };
    });

  if (data.length < 2) return null;

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <h3 className="text-sm font-semibold mb-4">Daily Average Glucose</h3>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={data} margin={{ top: 5, right: 5, bottom: 5, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(195, 15%, 88%)" vertical={false} />
          <ReferenceArea y1={70} y2={180} fill="hsl(152, 69%, 40%)" fillOpacity={0.06} />
          <ReferenceLine y={180} stroke="hsl(38, 92%, 50%)" strokeDasharray="4 4" strokeOpacity={0.4} />
          <ReferenceLine y={70} stroke="hsl(0, 72%, 51%)" strokeDasharray="4 4" strokeOpacity={0.4} />
          <XAxis
            dataKey="label"
            tick={{ fontSize: 10, fill: "hsl(210, 10%, 50%)" }}
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
            minTickGap={40}
          />
          <YAxis
            domain={[50, 300]}
            tick={{ fontSize: 10, fill: "hsl(210, 10%, 50%)" }}
            tickLine={false}
            axisLine={false}
            width={35}
          />
          <Tooltip content={<AvgTooltip />} />
          <Line
            type="monotone"
            dataKey="avg"
            stroke="hsl(168, 80%, 30%)"
            strokeWidth={2}
            dot={{ r: 3, fill: "hsl(168, 80%, 30%)" }}
            activeDot={{ r: 5 }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}