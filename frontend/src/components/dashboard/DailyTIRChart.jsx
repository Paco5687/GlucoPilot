import { ResponsiveContainer, AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine } from "recharts";
import { format } from "date-fns";

function TIRTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const d = payload[0]?.payload;
  if (!d) return null;
  return (
    <div className="bg-card border border-border rounded-lg shadow-lg p-2.5 text-xs">
      <p className="font-medium mb-1">{d.label}</p>
      <p className="text-green-600">In Range: {d.tir}%</p>
      <p className="text-amber-600">Above: {d.above}%</p>
      <p className="text-red-600">Below: {d.below}%</p>
      <p className="text-muted-foreground mt-1">Avg: {d.avg} mg/dL · {d.count} readings</p>
    </div>
  );
}

export default function DailyTIRChart({ readings }) {
  if (!readings?.length) return null;

  // Group by date
  const byDay = {};
  readings.forEach((r) => {
    const dayKey = format(new Date(r.timestamp), "yyyy-MM-dd");
    if (!byDay[dayKey]) byDay[dayKey] = [];
    byDay[dayKey].push(r.value);
  });

  const data = Object.entries(byDay)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, values]) => {
      const inRange = values.filter((v) => v >= 70 && v <= 180).length;
      const above = values.filter((v) => v > 180).length;
      const below = values.filter((v) => v < 70).length;
      const avg = Math.round(values.reduce((s, v) => s + v, 0) / values.length);
      return {
        day,
        label: format(new Date(day), "MMM d"),
        tir: Math.round((inRange / values.length) * 100),
        above: Math.round((above / values.length) * 100),
        below: Math.round((below / values.length) * 100),
        avg,
        count: values.length,
      };
    });

  if (data.length < 2) return null;

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold">Daily Time in Range</h3>
        <span className="text-xs text-muted-foreground">Target: 70%+</span>
      </div>
      <ResponsiveContainer width="100%" height={200}>
        <AreaChart data={data} margin={{ top: 5, right: 5, bottom: 5, left: 0 }}>
          <defs>
            <linearGradient id="tirGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="hsl(152, 69%, 40%)" stopOpacity={0.3} />
              <stop offset="100%" stopColor="hsl(152, 69%, 40%)" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(195, 15%, 88%)" vertical={false} />
          <ReferenceLine y={70} stroke="hsl(152, 69%, 40%)" strokeDasharray="6 3" strokeOpacity={0.5} />
          <XAxis
            dataKey="label"
            tick={{ fontSize: 10, fill: "hsl(210, 10%, 50%)" }}
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
            minTickGap={40}
          />
          <YAxis
            domain={[0, 100]}
            tick={{ fontSize: 10, fill: "hsl(210, 10%, 50%)" }}
            tickLine={false}
            axisLine={false}
            width={30}
            tickFormatter={(v) => `${v}%`}
          />
          <Tooltip content={<TIRTooltip />} />
          <Area
            type="monotone"
            dataKey="tir"
            stroke="hsl(152, 69%, 40%)"
            strokeWidth={2}
            fill="url(#tirGradient)"
            dot={{ r: 3, fill: "hsl(152, 69%, 40%)" }}
            activeDot={{ r: 5 }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}