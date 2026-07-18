export const RANGE = { low: 70, high: 180, veryHigh: 250 };

export function getGlucoseColor(value) {
  if (value < RANGE.low) return "glucose-low";
  if (value <= RANGE.high) return "glucose-in-range";
  if (value <= RANGE.veryHigh) return "glucose-high";
  return "glucose-very-high";
}

export function getGlucoseLabel(value) {
  if (value < RANGE.low) return "Low";
  if (value <= RANGE.high) return "In Range";
  if (value <= RANGE.veryHigh) return "High";
  return "Very High";
}

export function getGlucoseBgClass(value) {
  if (value < RANGE.low) return "bg-red-500/10 text-red-600 border-red-200";
  if (value <= RANGE.high) return "bg-green-500/10 text-green-600 border-green-200";
  if (value <= RANGE.veryHigh) return "bg-amber-500/10 text-amber-600 border-amber-200";
  return "bg-red-500/10 text-red-600 border-red-200";
}

export const TREND_ARROWS = {
  DoubleUp: "⇈",
  SingleUp: "↑",
  FortyFiveUp: "↗",
  Flat: "→",
  FortyFiveDown: "↘",
  SingleDown: "↓",
  DoubleDown: "⇊",
  Unknown: "?",
};

export const TREND_LABELS = {
  DoubleUp: "Rising rapidly",
  SingleUp: "Rising",
  FortyFiveUp: "Rising slowly",
  Flat: "Stable",
  FortyFiveDown: "Falling slowly",
  SingleDown: "Falling",
  DoubleDown: "Falling rapidly",
  Unknown: "Unknown",
};

export function calculateTimeInRange(readings) {
  if (!readings.length) return { inRange: 0, above: 0, below: 0 };
  const total = readings.length;
  const below = readings.filter((r) => r.value < RANGE.low).length;
  const above = readings.filter((r) => r.value > RANGE.high).length;
  const inRange = total - below - above;
  return {
    inRange: Math.round((inRange / total) * 100),
    above: Math.round((above / total) * 100),
    below: Math.round((below / total) * 100),
  };
}

export function calculateAverage(readings) {
  if (!readings.length) return 0;
  return Math.round(readings.reduce((s, r) => s + r.value, 0) / readings.length);
}

export function calculateStdDev(readings) {
  if (readings.length < 2) return 0;
  const avg = calculateAverage(readings);
  const variance = readings.reduce((s, r) => s + Math.pow(r.value - avg, 2), 0) / readings.length;
  return Math.round(Math.sqrt(variance));
}

export function calculateCV(readings) {
  const avg = calculateAverage(readings);
  const sd = calculateStdDev(readings);
  if (!avg) return 0;
  return Math.round((sd / avg) * 100);
}

export function calculateGMI(readings) {
  const avg = calculateAverage(readings);
  return (3.31 + 0.02392 * avg).toFixed(1);
}

export function formatTimeSince(date) {
  const diff = Date.now() - new Date(date).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ${mins % 60}m ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}