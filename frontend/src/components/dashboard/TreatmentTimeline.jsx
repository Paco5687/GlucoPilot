import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip } from "recharts";
import { format } from "date-fns";

function TimelineTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const d = payload[0]?.payload;
  if (!d) return null;
  return (
    <div className="bg-card border border-border rounded-lg shadow-lg p-2.5 text-xs">
      <p className="text-muted-foreground mb-1">{format(new Date(d.timestamp), "MMM d, h:mm a")}</p>
      {d.insulin > 0 && (
        <p className="font-mono text-blue-600">
          💉 {d.insulin.toFixed(2)} U
          {d.insulinLabel && <span className="text-muted-foreground ml-1">({d.insulinLabel})</span>}
        </p>
      )}
      {d.carbs > 0 && (
        <p className="font-mono text-orange-600">🍞 {d.carbs}g carbs</p>
      )}
    </div>
  );
}

export default function TreatmentTimeline({ treatments }) {
  if (!treatments?.length) return null;

  const insulin = treatments.filter(
    (t) => t.type === "insulin" && t.event_type !== "Daily Total" && t.amount
  );
  const carbs = treatments.filter((t) => t.type === "carb" && t.amount);

  if (!insulin.length && !carbs.length) return null;

  // Merge into timeline entries keyed by time (round to nearest 5 min)
  const timeMap = {};
  const roundTime = (ts) => {
    const d = new Date(ts);
    d.setMinutes(Math.round(d.getMinutes() / 5) * 5, 0, 0);
    return d.getTime();
  };

  insulin.forEach((t) => {
    const key = roundTime(t.timestamp);
    if (!timeMap[key]) timeMap[key] = { timestamp: key, insulin: 0, carbs: 0, insulinLabel: "" };
    timeMap[key].insulin += t.amount;
    timeMap[key].insulinLabel = t.event_type || "";
  });

  carbs.forEach((t) => {
    const key = roundTime(t.timestamp);
    if (!timeMap[key]) timeMap[key] = { timestamp: key, insulin: 0, carbs: 0, insulinLabel: "" };
    timeMap[key].carbs += t.amount;
  });

  const data = Object.values(timeMap).sort((a, b) => a.timestamp - b.timestamp);
  if (!data.length) return null;

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold">Insulin & Carb Timeline</h3>
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-blue-500" /> Insulin
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-orange-500" /> Carbs
          </span>
        </div>
      </div>
      <ResponsiveContainer width="100%" height={120}>
        <BarChart data={data} margin={{ top: 5, right: 5, bottom: 5, left: 0 }}>
          <XAxis
            dataKey="timestamp"
            tickFormatter={(t) => format(new Date(t), "HH:mm")}
            tick={{ fontSize: 10, fill: "hsl(210, 10%, 50%)" }}
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
            minTickGap={40}
          />
          <YAxis yAxisId="insulin" orientation="left" width={30} tick={{ fontSize: 9 }} tickLine={false} axisLine={false} label={{ value: "U", position: "insideTopLeft", fontSize: 9, fill: "hsl(210, 10%, 50%)" }} />
          <YAxis yAxisId="carbs" orientation="right" width={30} tick={{ fontSize: 9 }} tickLine={false} axisLine={false} label={{ value: "g", position: "insideTopRight", fontSize: 9, fill: "hsl(210, 10%, 50%)" }} />
          <Tooltip content={<TimelineTooltip />} />
          <Bar yAxisId="insulin" dataKey="insulin" fill="hsl(217, 91%, 60%)" radius={[3, 3, 0, 0]} barSize={8} />
          <Bar yAxisId="carbs" dataKey="carbs" fill="hsl(25, 95%, 53%)" radius={[3, 3, 0, 0]} barSize={8} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}