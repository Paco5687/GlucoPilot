// Time math for the Explorer: local-calendar-aware so "Sep 24" means her
// Sep 24 midnight-to-midnight, including across daylight-saving changes.

export const MIN = 60000;
export const HOUR = 60 * MIN;
export const DAY = 24 * HOUR;
export const MIN_SPAN = 15 * MIN;

export const SPAN_PRESETS = [
  ["6h", 6 * HOUR],
  ["24h", DAY],
  ["3d", 3 * DAY],
  ["7d", 7 * DAY],
  ["14d", 14 * DAY],
  ["30d", 30 * DAY],
  ["90d", 90 * DAY],
];

export function localMidnight(ms) {
  const d = new Date(ms);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

export function addLocalDays(ms, n) {
  const d = new Date(ms);
  d.setDate(d.getDate() + n);
  return d.getTime();
}

export function localDateValue(ms) {
  const d = new Date(ms), pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function localInputValue(ms) {
  const d = new Date(ms), pad = (n) => String(n).padStart(2, "0");
  return `${localDateValue(ms)}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "YYYY-MM-DD" -> local midnight ms, or null. */
export function parseLocalDate(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
  if (!m) return null;
  const t = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).getTime();
  return Number.isFinite(t) ? t : null;
}

/** "YYYY-MM-DDTHH:mm" (datetime-local) -> ms, or null for empty/partial input. */
export function parseLocalDateTime(value) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(String(value || ""))) return null;
  const t = new Date(value).getTime();
  return Number.isFinite(t) ? t : null;
}

/**
 * Normalize a requested view into the data bounds. Never returns NaN, never a
 * reversed window, never narrower than MIN_SPAN or wider than the data.
 * Returns null when the request itself is unusable.
 */
export function clampView(start, end, bounds) {
  if (!Number.isFinite(start) || !Number.isFinite(end) || !bounds) return null;
  if (end < start) [start, end] = [end, start];
  const full = Math.max(MIN_SPAN, bounds.end - bounds.start);
  if (end - start >= full) return { start: bounds.start, end: bounds.start + full };
  if (end - start < MIN_SPAN) {
    const center = (start + end) / 2;
    start = center - MIN_SPAN / 2;
    end = center + MIN_SPAN / 2;
  }
  const span = end - start;
  if (start < bounds.start) { start = bounds.start; end = start + span; }
  if (end > bounds.end) { end = bounds.end; start = Math.max(bounds.start, end - span); }
  return { start, end };
}

/** A calendar-day view: starts at local midnight and ends at the next one (or at "now" for today). */
export function isDayView(start, end, boundsEnd) {
  if (start !== localMidnight(start)) return false;
  const next = addLocalDays(start, 1);
  if (Math.abs(end - next) < MIN) return true;
  return end >= boundsEnd - MIN && end < next && boundsEnd < next;
}

/** The whole local day containing ms, clipped to "now" (boundsEnd). */
export function dayWindow(ms, boundsEnd) {
  const start = localMidnight(ms);
  return { start, end: Math.min(addLocalDays(start, 1), boundsEnd) };
}

export function matchSpanPreset(span) {
  const hit = SPAN_PRESETS.find(([, ms]) => Math.abs(span - ms) / ms < 0.03);
  return hit ? hit[0] : null;
}

export function durationText(span) {
  if (span < DAY * 2) {
    const h = Math.floor(span / HOUR), m = Math.round((span % HOUR) / MIN);
    return m && h < 12 ? `${h} h ${m} m` : `${Math.round(span / HOUR)} h`;
  }
  return `${Math.round(span / DAY)} days`;
}

const TICK_STEPS = [15 * MIN, 30 * MIN, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY];

/**
 * Axis ticks on round local clock times (…, 9:00, 12:00, …) or local
 * midnights, never the arbitrary fractions an even split produces.
 */
export function niceTicks(start, end, maxTicks) {
  const span = end - start;
  const step = TICK_STEPS.find((s) => span / s <= maxTicks) || TICK_STEPS[TICK_STEPS.length - 1];
  const ticks = [];
  if (step < DAY) {
    const stepMin = step / MIN;
    for (let day = localMidnight(start); day <= end; day = addLocalDays(day, 1)) {
      const base = new Date(day);
      for (let m = 0; m < 24 * 60; m += stepMin) {
        const t = new Date(base.getFullYear(), base.getMonth(), base.getDate(), 0, m).getTime();
        if (t >= start && t <= end && ticks[ticks.length - 1] !== t) ticks.push(t);
      }
    }
  } else {
    const days = Math.round(step / DAY);
    for (let t = localMidnight(start); t <= end; t = addLocalDays(t, days)) {
      if (t >= start) ticks.push(t);
    }
  }
  return { ticks, step };
}

export function localMidnightsBetween(start, end) {
  const out = [];
  for (let t = addLocalDays(localMidnight(start), 1); t <= end; t = addLocalDays(t, 1)) out.push(t);
  return out;
}

export function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
