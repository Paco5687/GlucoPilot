import { useMemo, useState } from "react";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";

const METRICS = [
  { key: "sleep_score", label: "Sleep", color: "#6366f1" },
  { key: "readiness_score", label: "Readiness", color: "#10b981" },
  { key: "activity_score", label: "Activity", color: "#f97316" },
  { key: "average_heart_rate", label: "Heart Rate", color: "#ef4444" },
];

export default function OuraTrendsChart({ data }) {
  const [activeMetrics, setActiveMetrics] = useState(["sleep_score", "readiness_score", "activity_score"]);

  const chartData = useMemo(() => {
    return [...data].reverse().map((d) => ({
      date: d.date,
      label: new Date(d.date + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" }),
      sleep_score: d.sleep_score,
      readiness_score: d.readiness_score,
      activity_score: d.activity_score,
      average_heart_rate: d.average_heart_rate,
    }));
  }, [data]);

  const toggleMetric = (key) => {
    setActiveMetrics((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]
    );
  };

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold">Oura Trends</h3>
        <div className="flex gap-1.5">
          {METRICS.map((m) => (
            <button
              key={m.key}
              onClick={() => toggleMetric(m.key)}
              className={`text-[10px] px-2 py-1 rounded-full font-medium transition-colors ${
                activeMetrics.includes(m.key)
                  ? "text-white"
                  : "bg-muted text-muted-foreground"
              }`}
              style={activeMetrics.includes(m.key) ? { backgroundColor: m.color } : {}}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>
      <div className="h-48">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis
              dataKey="label"
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              domain={["auto", "auto"]}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: "hsl(var(--card))",
                border: "1px solid hsl(var(--border))",
                borderRadius: "8px",
                fontSize: "12px",
              }}
            />
            {METRICS.filter((m) => activeMetrics.includes(m.key)).map((m) => (
              <Line
                key={m.key}
                type="monotone"
                dataKey={m.key}
                stroke={m.color}
                strokeWidth={2}
                dot={false}
                name={m.label}
                connectNulls
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}