import { describe, expect, it } from "vitest";
import {
  DAY, HOUR, MIN, MIN_SPAN, addLocalDays, clampView, dayWindow, durationText, esc, isDayView,
  localMidnight, matchSpanPreset, niceTicks, parseLocalDate, parseLocalDateTime,
} from "./explorerTime";

const BOUNDS = { start: new Date(2026, 3, 1).getTime(), end: new Date(2026, 8, 27, 20, 0).getTime() };

describe("clampView", () => {
  it("rejects NaN instead of producing a blank chart", () => {
    expect(clampView(NaN, BOUNDS.end, BOUNDS)).toBeNull();
    expect(clampView(BOUNDS.start, Number.NaN, BOUNDS)).toBeNull();
  });
  it("swaps a start that was typed after the end", () => {
    const a = new Date(2026, 8, 10).getTime(), b = new Date(2026, 8, 12).getTime();
    expect(clampView(b, a, BOUNDS)).toEqual({ start: a, end: b });
  });
  it("widens a too-narrow window around its center", () => {
    const t = new Date(2026, 8, 10, 12).getTime();
    const v = clampView(t, t + MIN, BOUNDS);
    expect(v.end - v.start).toBe(MIN_SPAN);
    expect((v.start + v.end) / 2).toBe(t + MIN / 2);
  });
  it("pins a window that runs past now to the latest data, keeping its span", () => {
    const v = clampView(BOUNDS.end - HOUR, BOUNDS.end + 5 * HOUR, BOUNDS);
    expect(v).toEqual({ start: BOUNDS.end - 6 * HOUR, end: BOUNDS.end });
  });
  it("collapses an oversized request to all data", () => {
    expect(clampView(0, BOUNDS.end * 2, BOUNDS)).toEqual(BOUNDS);
  });
});

describe("input parsing", () => {
  it("treats partial and empty datetime input as unusable", () => {
    expect(parseLocalDateTime("")).toBeNull();
    expect(parseLocalDateTime("2026-09-2")).toBeNull();
    expect(parseLocalDateTime("2026-09-25T10:03")).toBe(new Date(2026, 8, 25, 10, 3).getTime());
  });
  it("reads a date picker value as local midnight", () => {
    expect(parseLocalDate("2026-09-25")).toBe(new Date(2026, 8, 25).getTime());
    expect(parseLocalDate("nope")).toBeNull();
  });
});

describe("day views", () => {
  it("a past day spans local midnight to local midnight", () => {
    const w = dayWindow(new Date(2026, 8, 25, 10, 3).getTime(), BOUNDS.end);
    expect(w).toEqual({ start: new Date(2026, 8, 25).getTime(), end: new Date(2026, 8, 26).getTime() });
    expect(isDayView(w.start, w.end, BOUNDS.end)).toBe(true);
  });
  it("today ends at now and is still a day view", () => {
    const w = dayWindow(BOUNDS.end, BOUNDS.end);
    expect(w).toEqual({ start: localMidnight(BOUNDS.end), end: BOUNDS.end });
    expect(isDayView(w.start, w.end, BOUNDS.end)).toBe(true);
  });
  it("an arbitrary 24h window is not a day view", () => {
    const s = new Date(2026, 8, 25, 6).getTime();
    expect(isDayView(s, s + DAY, BOUNDS.end)).toBe(false);
  });
  it("stepping days follows the calendar", () => {
    const d = new Date(2026, 8, 25).getTime();
    expect(new Date(addLocalDays(d, 1)).getDate()).toBe(26);
    expect(new Date(addLocalDays(d, -1)).getDate()).toBe(24);
  });
});

describe("niceTicks", () => {
  it("puts ticks on round local clock times for a day", () => {
    const s = new Date(2026, 8, 25, 0, 7).getTime();
    const { ticks, step } = niceTicks(s, s + DAY, 12);
    expect(step).toBe(2 * HOUR);
    for (const t of ticks) {
      const d = new Date(t);
      expect(d.getMinutes()).toBe(0);
      expect(d.getHours() % 2).toBe(0);
    }
    expect(ticks.length).toBeLessThanOrEqual(13);
  });
  it("uses local midnights for multi-week spans", () => {
    const s = new Date(2026, 8, 1, 9).getTime();
    const { ticks } = niceTicks(s, s + 14 * DAY, 8);
    for (const t of ticks) expect(t).toBe(localMidnight(t));
  });
});

describe("labels and escaping", () => {
  it("names the span presets it recognises", () => {
    expect(matchSpanPreset(DAY)).toBe("24h");
    expect(matchSpanPreset(7 * DAY)).toBe("7d");
    expect(matchSpanPreset(5 * HOUR)).toBeNull();
  });
  it("describes durations plainly", () => {
    expect(durationText(DAY)).toBe("24 h");
    expect(durationText(6 * HOUR + 30 * MIN)).toBe("6 h 30 m");
    expect(durationText(14 * DAY)).toBe("14 days");
  });
  it("escapes third-party text before it becomes HTML", () => {
    expect(esc('<img src=x onerror="alert(1)">')).toBe("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
  });
});
