import { useMemo } from "react";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, ReferenceLine } from "recharts";

function formatTime(ts) {
  const d = new Date(ts);
  return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

function formatDateShort(ts) {
  const d = new Date(ts);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function HRTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="bg-card border border-border rounded-lg px-3 py-2 text-xs shadow-lg">
      <p className="font-medium mb-1">{new Date(d.timestamp).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</p>
      <p className="text-red-500">♥ {d.bpm} bpm</p>
    </div>
  );
}

export default function OuraHeartRateChart({ data }) {
  const chartData = useMemo(() => {
    if (!data?.length) return [];
    return [...data]
      .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
      .map((d) => ({
        timestamp: d.timestamp,
        time: new Date(d.timestamp).getTime(),
        bpm: d.bpm,
      }));
  }, [data]);

  if (!chartData.length) {
    return (
      <div className="bg-card rounded-xl border border-border p-4">
        <h3 className="text-sm font-semibold mb-2">Heart Rate</h3>
        <p className="text-xs text-muted-foreground text-center py-8">No intraday heart rate data. Sync Oura to load.</p>
      </div>
    );
  }

  // Determine if data spans multiple days for axis formatting
  const spanMs = chartData[chartData.length - 1].time - chartData[0].time;
  const multiDay = spanMs > 24 * 3600000;

  // Compute average for reference line
  const avgBpm = Math.round(chartData.reduce((s, d) => s + d.bpm, 0) / chartData.length);

  // Oura is not real-time: the ring uploads when the app syncs. Label how old
  // the newest sample is so a lagging window isn't mistaken for missing data.
  const newestMs = chartData[chartData.length - 1].time;
  const ageMin = Math.round((Date.now() - newestMs) / 60000);
  const staleness =
    ageMin >= 90 ? `data ends ${Math.round(ageMin / 60)} h ago` : ageMin >= 20 ? `data ends ${ageMin} min ago` : null;

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold">Heart Rate</h3>
        <span className="text-xs text-muted-foreground">
          {staleness ? `${staleness} (ring syncs periodically) · ` : ""}Avg: {avgBpm} bpm
        </span>
      </div>
      <div className="h-48">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis
              dataKey="time"
              type="number"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(t) => multiDay ? formatDateShort(t) : formatTime(t)}
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              domain={["auto", "auto"]}
              unit=" bpm"
            />
            <Tooltip content={<HRTooltip />} />
            {chartData.length >= 10 && (
              <ReferenceLine y={avgBpm} stroke="#ef4444" strokeDasharray="4 4" strokeOpacity={0.5} />
            )}
            <Line
              type="monotone"
              dataKey="bpm"
              stroke="#ef4444"
              strokeWidth={1.5}
              dot={chartData.length < 20 ? { r: 3, fill: "#ef4444" } : false}
              name="Heart Rate"
              connectNulls
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}