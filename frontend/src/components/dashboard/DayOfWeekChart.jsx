import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine } from "recharts";

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export default function DayOfWeekChart({ readings }) {
  if (!readings.length) return null;

  const byDay = {};
  DAYS.forEach((d, i) => (byDay[i] = []));
  readings.forEach((r) => {
    const day = new Date(r.timestamp).getDay();
    byDay[day].push(r.value);
  });

  const data = DAYS.map((name, i) => {
    const vals = byDay[i];
    if (!vals.length) return { name, avg: 0, min: 0, max: 0 };
    const avg = Math.round(vals.reduce((s, v) => s + v, 0) / vals.length);
    return {
      name,
      avg,
      min: Math.min(...vals),
      max: Math.max(...vals),
    };
  });

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <h3 className="text-sm font-semibold mb-4">Day-of-Week Averages</h3>
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={data} margin={{ top: 5, right: 5, bottom: 5, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(195, 15%, 88%)" vertical={false} />
          <ReferenceLine y={180} stroke="hsl(38, 92%, 50%)" strokeDasharray="4 4" strokeOpacity={0.4} />
          <ReferenceLine y={70} stroke="hsl(0, 72%, 51%)" strokeDasharray="4 4" strokeOpacity={0.4} />
          <XAxis dataKey="name" tick={{ fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis domain={[40, 280]} tick={{ fontSize: 10 }} tickLine={false} axisLine={false} width={35} />
          <Tooltip
            contentStyle={{ borderRadius: 8, border: "1px solid hsl(195, 15%, 88%)", fontSize: 12 }}
            formatter={(v) => [`${v} mg/dL`, "Average"]}
          />
          <Bar dataKey="avg" fill="hsl(168, 80%, 30%)" radius={[4, 4, 0, 0]} barSize={28} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}