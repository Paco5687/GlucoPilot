import { localMidnight } from "./explorerTime";

// Shapes GlucoseReading/Treatment rows into what the Explorer canvas draws.

const parseT = (v) => new Date(v).getTime();

export function buildChartData(readings, treatments, now = Date.now()) {
  const timeline = readings
    .filter((r) => r.value != null && r.timestamp)
    .map((r) => ({ ms: parseT(r.timestamp), bg: r.value, source: r.source }))
    .filter((r) => Number.isFinite(r.ms))
    .sort((a, b) => a.ms - b.ms);
  for (let i = 0; i < timeline.length; i++) {
    timeline[i].prevBg = i > 0 ? timeline[i - 1].bg : null;
    timeline[i].prevMs = i > 0 ? timeline[i - 1].ms : null;
    timeline[i].nextBg = i < timeline.length - 1 ? timeline[i + 1].bg : null;
    timeline[i].nextMs = i < timeline.length - 1 ? timeline[i + 1].ms : null;
  }

  const byType = (t) => treatments.filter((x) => x.type === t && x.timestamp);

  const carbEvents = byType("carb").map((t) => ({ ms: parseT(t.timestamp), amount: Number(t.amount) || 0 }));
  // Pump daily totals share type "insulin" but are a day's sum stamped at
  // noon, not a dose; drawing them made a phantom bolus every morning.
  const boluses = byType("insulin")
    .filter((t) => t.event_type !== "Daily Total")
    .map((t) => {
      const ms = parseT(t.timestamp);
      // attach carbs logged within ±10 min (they were one row in the old CSV data)
      const carbs = carbEvents
        .filter((c) => Math.abs(c.ms - ms) <= 10 * 60000)
        .reduce((s, c) => s + c.amount, 0);
      return {
        ms,
        amount: Number(t.amount) || 0,
        carbs,
        description: t.event_type || "Bolus",
        details: t.notes || "",
        unit: "U",
        type: "insulin",
        insulin_type: t.insulin_type,
        source: t.source,
      };
    })
    .sort((a, b) => a.ms - b.ms);

  // Glooko "tempbasal" rows are the programmed schedule (the reconciler reads
  // them the same way); the actual temporary basals are
  // "tempbasal_correction". Scheduled rows give IOB its baseline rate.
  const basalEvents = [...byType("tempbasal"), ...byType("tempbasal_correction"), ...byType("suspension")]
    .map((t) => {
      const scheduled = t.type === "tempbasal" && t.source === "glooko";
      return {
        ms: parseT(t.timestamp),
        amount: Number(t.absolute ?? t.amount) || 0,
        duration: Number(t.duration) || 0,
        multiplier: t.multiplier != null ? Number(t.multiplier) : null,
        description: t.type === "suspension" ? "Suspend" : scheduled ? "Scheduled basal" : "Temporary basal",
        details: t.notes || "",
        type: t.type,
        source: t.source,
      };
    })
    .sort((a, b) => a.ms - b.ms);
  // Each temp/suspend carries the scheduled rate in force when it began, so
  // IOB can take the delta without rescanning the schedule every frame. A
  // percentage temp basal states its own baseline exactly (rate ÷
  // multiplier); the schedule lookup is only the fallback, because the pump
  // logs brief 0-rate segments between temps that would otherwise count a
  // correction's whole rate as extra insulin.
  let scheduledRate = null;
  for (const e of basalEvents) {
    if (e.description === "Scheduled basal") scheduledRate = e.amount;
    else e.scheduledRate = e.multiplier > 0 ? e.amount / e.multiplier : scheduledRate;
  }

  const alarms = byType("note")
    .filter((t) => /alarm|alert/i.test(t.event_type || ""))
    .map((t) => ({ ms: parseT(t.timestamp), description: t.event_type, details: t.notes || "", type: "alarm" }));

  const manualBg = byType("bg")
    .map((t) => ({ ms: parseT(t.timestamp), amount: Number(t.glucose) || 0, description: "Manual BG", type: "bg" }))
    .filter((t) => t.amount > 0);

  const timelineBoluses = boluses.map((b) => ({ ms: b.ms, amount: b.amount, carbs: b.carbs }));

  // Loop rather than Math.max(...array): spreading tens of thousands of
  // values into one call eventually exceeds the engine's argument limit.
  let first = Infinity, last = -Infinity;
  for (const d of timeline) { if (d.ms < first) first = d.ms; if (d.ms > last) last = d.ms; }
  for (const b of boluses) { if (b.ms < first) first = b.ms; if (b.ms > last) last = b.ms; }
  // The range reaches "now" so today is always navigable, but `latest` is the
  // newest actual data: that is where the chart opens and where "Latest" goes,
  // so a lagging feed never greets her with an empty window.
  const bounds = Number.isFinite(first)
    ? { start: first, end: Math.max(last, now), latest: last }
    : { start: now - 864e5, end: now, latest: now };

  // Per local day: coverage, time in range, lows — for the overview strip.
  const days = new Map();
  for (const d of timeline) {
    const key = localMidnight(d.ms);
    let s = days.get(key);
    if (!s) days.set(key, (s = { start: key, n: 0, inRange: 0, low: 0, sum: 0 }));
    s.n += 1;
    s.sum += d.bg;
    if (d.bg >= 70 && d.bg <= 180) s.inRange += 1;
    if (d.bg < 70) s.low += 1;
  }
  const daily = [...days.values()]
    .sort((a, b) => a.start - b.start)
    .map((s) => ({
      start: s.start,
      n: s.n,
      tir: Math.round((100 * s.inRange) / s.n),
      lowPct: Math.round((100 * s.low) / s.n),
      mean: Math.round(s.sum / s.n),
    }));

  return {
    timeline, boluses, timelineBoluses, basalEvents, alarms, manualBg, bounds, daily,
    counts: { readings: timeline.length, treatments: treatments.length },
  };
}
