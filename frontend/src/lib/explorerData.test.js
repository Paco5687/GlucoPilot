import { describe, expect, it } from "vitest";
import { buildChartData } from "./explorerData";

const NOW = new Date(2026, 8, 27, 20).getTime();
const at = (d, h, m = 0) => new Date(2026, 8, d, h, m).toISOString();

describe("buildChartData", () => {
  it("does not draw pump daily totals as boluses", () => {
    const data = buildChartData(
      [{ timestamp: at(25, 9), value: 120 }],
      [
        { type: "insulin", event_type: "Daily Total", timestamp: at(25, 8), notes: "Total: 38U" },
        { type: "insulin", event_type: "manual", timestamp: at(25, 10, 3), amount: 0.25 },
      ],
      NOW,
    );
    expect(data.boluses.map((b) => b.description)).toEqual(["manual"]);
  });

  it("separates the scheduled basal from actual temp basal corrections", () => {
    const data = buildChartData([{ timestamp: at(25, 9), value: 120 }], [
      { type: "tempbasal", source: "glooko", timestamp: at(25, 8), absolute: 2.35, duration: 240 },
      { type: "tempbasal_correction", source: "glooko", timestamp: at(25, 10), absolute: 4.58, multiplier: 1.95, duration: 90 },
      { type: "tempbasal", source: "tandem", timestamp: at(25, 14), absolute: 1.2, duration: 30 },
    ], NOW);
    const byDesc = Object.fromEntries(data.basalEvents.map((e) => [e.type + ":" + e.source, e]));
    expect(byDesc["tempbasal:glooko"].description).toBe("Scheduled basal");
    expect(byDesc["tempbasal_correction:glooko"].description).toBe("Temporary basal");
    expect(byDesc["tempbasal_correction:glooko"].multiplier).toBe(1.95);
    expect(byDesc["tempbasal:tandem"].description).toBe("Temporary basal");
    // The correction knows the scheduled rate it raised, for the IOB delta.
    expect(byDesc["tempbasal_correction:glooko"].scheduledRate).toBe(2.35);
  });

  it("extends the range to now and survives large datasets", () => {
    const readings = [];
    const t0 = new Date(2026, 0, 1).getTime();
    for (let i = 0; i < 150000; i++) readings.push({ timestamp: new Date(t0 + i * 300000).toISOString(), value: 110 });
    const data = buildChartData(readings, [], NOW);
    expect(data.bounds.start).toBe(t0);
    expect(data.bounds.end).toBe(Math.max(NOW, t0 + 149999 * 300000));
    expect(data.bounds.latest).toBe(t0 + 149999 * 300000);
  });

  it("summarises each local day for the overview strip", () => {
    const data = buildChartData(
      [{ timestamp: at(25, 1), value: 60 }, { timestamp: at(25, 2), value: 100 }, { timestamp: at(25, 3), value: 200 }, { timestamp: at(25, 4), value: 150 }],
      [],
      NOW,
    );
    expect(data.daily).toHaveLength(1);
    expect(data.daily[0]).toMatchObject({ start: new Date(2026, 8, 25).getTime(), n: 4, tir: 50, lowPct: 25, mean: 128 });
  });
});
