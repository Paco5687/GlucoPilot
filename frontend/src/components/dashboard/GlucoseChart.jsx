import { ResponsiveContainer, ComposedChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine, ReferenceArea, ReferenceDot, Symbols } from "recharts";
import { format, differenceInHours, parseISO } from "date-fns";

function FingerstickNote({ f }) {
  const up = f.delta > 0;
  return (
    <div className="mt-2 pt-2 border-t border-border">
      <p className="font-mono font-bold">
        🩸 {f.value} <span className="text-xs text-muted-foreground">mg/dL fingerstick · {format(new Date(f.timestamp), "h:mm a")}</span>
      </p>
      {f.cgm_value != null ? (
        <p className="text-xs mt-1">
          CGM read <b>{f.cgm_value}</b> ·{" "}
          <span className={Math.abs(f.delta) >= 20 ? "text-red-600 font-medium" : "text-muted-foreground"}>
            Δ {up ? "+" : ""}{f.delta} {up ? "(CGM high)" : "(CGM low)"}
          </span>
        </p>
      ) : (
        <p className="text-xs mt-1 text-muted-foreground">no CGM reading nearby</p>
      )}
    </div>
  );
}

function CustomTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  const ts = d.timestamp ? new Date(d.timestamp) : null;
  if (!ts || isNaN(ts.getTime())) return null;
  return (
    <div className="bg-card border border-border rounded-lg shadow-lg p-3 text-sm">
      <p className="font-mono font-bold text-lg">{d.value} <span className="text-xs text-muted-foreground">mg/dL</span></p>
      <p className="text-muted-foreground text-xs">{format(ts, "MMM d, h:mm a")}</p>
      {d.insulinAmount > 0 && (
        <p className="text-blue-600 text-xs mt-1">💉 {d.insulinAmount.toFixed(2)} U</p>
      )}
      {d.carbAmount > 0 && (
        <p className="text-orange-600 text-xs">🍞 {d.carbAmount}g carbs</p>
      )}
      {d.fingersticks.map((f, i) => <FingerstickNote key={i} f={f} />)}
    </div>
  );
}

function FingerstickDiamond({ cx, cy }) {
  return <Symbols cx={cx} cy={cy} type="diamond" size={20} fill="hsl(292, 84%, 48%)" stroke="hsl(0, 0%, 100%)" strokeWidth={1} />;
}

const PHASE_COLORS = {
  menstrual: "rgba(239, 68, 68, 0.06)",
  follicular: "rgba(236, 72, 153, 0.06)",
  ovulation: "rgba(168, 85, 247, 0.06)",
  luteal: "rgba(245, 158, 11, 0.06)",
};

export default function GlucoseChart({ readings, treatments, periodLogs, fingersticks }) {
  if (!readings.length) return null;

  const allSorted = [...readings].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
  const spanHours = allSorted.length > 1
    ? differenceInHours(new Date(allSorted[allSorted.length - 1].timestamp), new Date(allSorted[0].timestamp))
    : 1;

  // Downsample for readability: keep every Nth point based on time span
  const step = spanHours > 168 ? 6 : spanHours > 72 ? 3 : spanHours > 24 ? 2 : 1;
  const downsampled = step > 1
    ? allSorted.filter((_, i) => i % step === 0)
    : allSorted;

  // Date format based on time span
  const xFormat = spanHours > 48 ? "MMM d" : spanHours > 24 ? "d HH:mm" : "HH:mm";

  const sorted = downsampled.map((r) => {
    const time = new Date(r.timestamp).getTime();
    return {
      ...r,
      time,
      timeLabel: format(new Date(r.timestamp), xFormat),
      insulinAmount: 0,
      carbAmount: 0,
      fingersticks: [],
    };
  });
  const chartStart = sorted[0]?.time;
  const chartEnd = sorted[sorted.length - 1]?.time;

  // The reading a marker's details show under in the tooltip: the closest one,
  // if it is within one (downsampled) reading interval.
  const matchWindow = step * 5 * 60000;
  const nearestReading = (time) => {
    let lo = 0;
    let hi = sorted.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (sorted[mid].time < time) lo = mid + 1;
      else hi = mid;
    }
    const i = lo > 0 && time - sorted[lo - 1].time < sorted[lo].time - time ? lo - 1 : lo;
    return Math.abs(sorted[i].time - time) <= matchWindow ? sorted[i] : null;
  };

  // Period phase bands
  const phaseBands = [];
  if (periodLogs?.length) {
    const chartMin = sorted[0]?.time;
    const chartMax = sorted[sorted.length - 1]?.time;
    const sortedLogs = [...periodLogs].sort((a, b) => a.date.localeCompare(b.date));
    let bandStart = null;
    let bandPhase = null;
    for (const log of sortedLogs) {
      if (!log.phase) continue;
      const dayStart = parseISO(log.date).getTime();
      const dayEnd = dayStart + 86400000;
      if (dayEnd < chartMin || dayStart > chartMax) continue;
      if (bandPhase === log.phase && bandStart) {
        bandStart.end = dayEnd;
      } else {
        if (bandStart) phaseBands.push(bandStart);
        bandStart = { phase: log.phase, start: dayStart, end: dayEnd };
        bandPhase = log.phase;
      }
    }
    if (bandStart) phaseBands.push(bandStart);
  }

  // Markers are ReferenceDots, not Scatter series. A series with its own `data`
  // replaces the chart data in Recharts' axis and tooltip lookup, which pinned
  // the hover tooltip to the markers' positions and showed the wrong reading.
  // Treatment markers sit at the top (insulin) and bottom (carbs) of the chart.
  const insulinMarkers = (treatments || [])
    .filter((t) => t.type === "insulin" && t.amount && t.event_type !== "Daily Total")
    .map((t) => ({ time: new Date(t.timestamp).getTime(), amount: t.amount }))
    .filter((m) => !Number.isNaN(m.time));
  const carbMarkers = (treatments || [])
    .filter((t) => t.type === "carb" && t.amount)
    .map((t) => ({ time: new Date(t.timestamp).getTime(), amount: t.amount }))
    .filter((m) => !Number.isNaN(m.time));

  // Fingerstick "correction points" — plotted at the meter value (on the y-axis),
  // overlaid on the CGM trace to mark where the graph disagreed. Never replaces it.
  const fingerstickMarkers = (fingersticks || [])
    .map((f) => ({ ...f, time: new Date(f.timestamp).getTime() }))
    .filter((m) => !Number.isNaN(m.time) && m.time >= chartStart && m.time <= chartEnd);

  for (const m of insulinMarkers) {
    const r = nearestReading(m.time);
    if (r) r.insulinAmount += m.amount;
  }
  for (const m of carbMarkers) {
    const r = nearestReading(m.time);
    if (r) r.carbAmount += m.amount;
  }
  for (const m of fingerstickMarkers) {
    nearestReading(m.time)?.fingersticks.push(m);
  }

  // Axes cover the readings plus every marker. Recharts still widens the y-axis
  // for readings outside 40–320.
  const treatmentTimes = [...insulinMarkers, ...carbMarkers].map((m) => m.time);
  const fingerstickValues = fingerstickMarkers.map((m) => m.value);
  const xDomain = [Math.min(chartStart, ...treatmentTimes), Math.max(chartEnd, ...treatmentTimes)];
  const yDomain = [Math.min(40, ...fingerstickValues), Math.max(320, ...fingerstickValues)];

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold">Glucose Trend</h3>
        <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-green-500" /> In Range
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-amber-500" /> High
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-red-500" /> Low
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-sm bg-blue-500" /> Insulin
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-orange-500" /> Carbs
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rotate-45 bg-fuchsia-600" /> Fingerstick
          </span>
        </div>
      </div>
      <ResponsiveContainer width="100%" height={300}>
        <ComposedChart data={sorted} margin={{ top: 20, right: 10, bottom: 5, left: 0 }}>
          <defs>
            <linearGradient id="glucoseGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="hsl(168, 80%, 30%)" stopOpacity={0.2} />
              <stop offset="100%" stopColor="hsl(168, 80%, 30%)" stopOpacity={0.01} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(195, 15%, 88%)" vertical={false} />
          <ReferenceArea y1={70} y2={180} fill="hsl(152, 69%, 40%)" fillOpacity={0.05} />
          {phaseBands.map((band, i) => (
            <ReferenceArea key={`phase-${i}`} x1={band.start} x2={band.end} fill={PHASE_COLORS[band.phase] || "transparent"} />
          ))}
          <ReferenceLine y={180} stroke="hsl(38, 92%, 50%)" strokeDasharray="4 4" strokeOpacity={0.4} label={{ value: "180", position: "right", fontSize: 9, fill: "hsl(38, 92%, 50%)" }} />
          <ReferenceLine y={70} stroke="hsl(0, 72%, 51%)" strokeDasharray="4 4" strokeOpacity={0.4} label={{ value: "70", position: "right", fontSize: 9, fill: "hsl(0, 72%, 51%)" }} />
          <XAxis
            dataKey="time"
            type="number"
            domain={xDomain}
            tickFormatter={(t) => format(new Date(t), xFormat)}
            tick={{ fontSize: 10, fill: "hsl(210, 10%, 50%)" }}
            tickLine={false}
            axisLine={false}
            minTickGap={60}
          />
          <YAxis
            domain={yDomain}
            tick={{ fontSize: 10, fill: "hsl(210, 10%, 50%)" }}
            tickLine={false}
            axisLine={false}
            width={35}
            ticks={[40, 70, 110, 180, 250, 320]}
          />
          <Tooltip content={<CustomTooltip />} />
          <Area
            type="monotone"
            dataKey="value"
            stroke="hsl(168, 80%, 30%)"
            strokeWidth={1.5}
            fill="url(#glucoseGradient)"
            dot={false}
            activeDot={{ r: 3, fill: "hsl(168, 80%, 30%)" }}
            isAnimationActive={false}
          />
          {insulinMarkers.map((m, i) => (
            <ReferenceDot key={`insulin-${i}`} x={m.time} y={305} r={2.5} fill="hsl(217, 91%, 60%)" fillOpacity={0.7} stroke="none" />
          ))}
          {carbMarkers.map((m, i) => (
            <ReferenceDot key={`carb-${i}`} x={m.time} y={55} r={2.5} fill="hsl(25, 95%, 53%)" fillOpacity={0.7} stroke="none" />
          ))}
          {fingerstickMarkers.map((m, i) => (
            <ReferenceDot key={`fingerstick-${i}`} x={m.time} y={m.value} shape={FingerstickDiamond} />
          ))}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}