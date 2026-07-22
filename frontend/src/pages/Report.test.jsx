import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Report from "./Report";

const report = {
  days: 90,
  start_date: "2026-02-01",
  end_date: "2026-05-01",
  generated_at: "2026-05-01T12:00:00Z",
  glucose: {
    available: true,
    quality: {},
    avg: 121,
    gmi: 6.2,
    tir: 80,
    cv: 31,
    readings: 100,
    days: 1,
    tbr54: 1,
    tbr70: 4,
    tar180: 16,
    tar250: 3,
    agp: [],
  },
  insulin: { available: false, quality: {}, nutrition_quality: {} },
  cycle: { available: false, quality: {} },
  wellness: { oura: null, fitbit: null, quality: {} },
  labs: { available: false, categories: {}, flagged: [], verification: {} },
  contradictions: {
    unresolved: [{
      id: "contr_report_synthetic",
      domain: "pump_tdd",
      severity: "blocking",
      resolution_state: "unresolved",
      detection_state: "active",
      explanation: "Synthetic pump totals disagree.",
      left: { label: "Pump reported", value: 30, unit: "units/day", observed_at: "2026-04-30" },
      right: { label: "Calculated from delivery events", value: 24, unit: "units/day", observed_at: "2026-04-30" },
    }],
  },
  narrative: null,
};

describe("visit report contradictions", () => {
  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => report,
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("prints both sides of every unresolved conflict", async () => {
    render(<Report />);

    expect(await screen.findByText("Unresolved data contradictions")).toBeTruthy();
    expect(screen.getByText("Synthetic pump totals disagree.")).toBeTruthy();
    expect(screen.getByText("30 units/day")).toBeTruthy();
    expect(screen.getByText("24 units/day")).toBeTruthy();
    expect(screen.getByText(/No conflicting value has been selected silently/)).toBeTruthy();
  });
});
