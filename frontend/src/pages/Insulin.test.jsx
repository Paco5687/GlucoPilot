import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Insulin from "./Insulin";

const apiMocks = vi.hoisted(() => ({ invoke: vi.fn() }));

async function invokeImplementation(_name, body) {
  if (body.action === "resistance") {
    return {
      data: {
        available: true,
        current: true,
        category: "typical",
        tdd_per_kg: 0.5,
        weight_kg: 70,
        avg_tdd: 35,
        n_days: 30,
        basal_pct: 60,
        est_isf_mgdl_per_u: 51,
        est_carb_ratio_g_per_u: 14.3,
        quality: {},
        reconciliation: {},
        per_phase_tdd_per_kg: {},
        series: [
          { date: "2026-08-30", tdd_per_kg: 0.42, weight_kg: 90.0, avg_tdd: 38.1, avg_basal: 25.0, avg_bolus: 13.1, days: 7 },
          { date: "2026-09-06", tdd_per_kg: 0.45, weight_kg: 86.0, avg_tdd: 38.9, avg_basal: 25.4, avg_bolus: 13.5, days: 7 },
          { date: "2026-09-13", tdd_per_kg: 0.47, weight_kg: 83.0, avg_tdd: 39.2, avg_basal: 25.8, avg_bolus: 13.4, days: 7 },
          { date: "2026-09-20", tdd_per_kg: 0.49, weight_kg: 80.0, avg_tdd: 39.0, avg_basal: 26.0, avg_bolus: 13.0, days: 7 },
        ],
        correction_series: [
          { date: "2026-08-30", n: 9, n_temp_basal: 9, n_bolus: 0, units_temp_basal: 24.1, units_bolus: 0, drop_per_unit: 4.0, drop_per_unit_temp_basal: 4.0, drop_per_unit_bolus: null, median_start_glucose: 118, confident: true },
          { date: "2026-09-06", n: 12, n_temp_basal: 11, n_bolus: 1, units_temp_basal: 30.2, units_bolus: 1.5, drop_per_unit: 7.0, drop_per_unit_temp_basal: 7.0, drop_per_unit_bolus: 20.0, median_start_glucose: 121, confident: true },
          { date: "2026-09-13", n: 15, n_temp_basal: 13, n_bolus: 2, units_temp_basal: 35.0, units_bolus: 3.0, drop_per_unit: 10.0, drop_per_unit_temp_basal: 9.5, drop_per_unit_bolus: 18.0, median_start_glucose: 124, confident: true },
        ],
        correction_summary: { n_total: 40, n_clean: 36, n_confounded: 4, n_temp_basal: 33, n_bolus: 3, drop_per_unit_temp_basal: 6.0, drop_per_unit_bolus: 19.0, drop_per_unit_from_high: 21.0, drop_per_unit_from_low: 5.0, median_start_glucose: 120 },
      },
    };
  }
  return {
    data: {
      available: true,
      algorithm_version: "insulin-response/1.0.0",
      window_days: 120,
      response_window_minutes: 120,
      counts: { total: 12, clean: 8, confounded: 3, excluded: 1 },
      reason_counts: { carbohydrate_in_response_window: 3, missing_end_glucose: 1 },
      n: 8,
      consistency: "variable",
      cv_pct: 38,
      median_drop_per_unit: 24,
      mean_drop_per_unit: 25,
      min_drop_per_unit: 10,
      max_drop_per_unit: 45,
      expected_isf: 51,
      quality: {},
      confidence: {
        discovery_status: "exploratory",
        confidence_label: "low",
      },
      analysis: {
        strata: {
          time_of_day: [{
            value: "morning",
            sample_count: 4,
            median_nadir_drop_per_unit_mg_dl: 23,
          }],
          cycle_phase: [],
          activity: [],
          position: [],
        },
      },
    },
  };
}

vi.mock("@/api/base44Client", () => ({
  base44: { functions: { invoke: apiMocks.invoke } },
}));

vi.mock("../components/SafetyBanner", () => ({ default: () => null }));
// jsdom has no layout engine or ResizeObserver; render chart shells as plain divs.
vi.mock("recharts", () => {
  const Box = ({ children }) => <div>{children}</div>;
  const Nothing = () => null;
  return {
    ResponsiveContainer: Box, LineChart: Box, AreaChart: Box, BarChart: Box,
    Line: Nothing, Area: Nothing, Bar: Nothing, XAxis: Nothing, YAxis: Nothing,
    Tooltip: Nothing, ReferenceArea: Nothing, CartesianGrid: Nothing, Legend: Nothing,
  };
});
vi.mock("@/components/ContradictionPanel", () => ({ default: () => null }));

beforeEach(() => {
  apiMocks.invoke.mockImplementation(invokeImplementation);
});

afterEach(() => {
  cleanup();
  apiMocks.invoke.mockReset();
});

describe("Insulin page", () => {
  it("leads with the headline numbers in plain language", async () => {
    render(<Insulin />);

    expect(await screen.findByText("How much you use")).toBeTruthy();
    expect(screen.getByText("35")).toBeTruthy(); // avg TDD
    expect(screen.getByText(/60% basal · 40% bolus/)).toBeTruthy();
    expect(screen.getByText(/at 154 lb/)).toBeTruthy(); // 70 kg shown in pounds
    expect(screen.getByText("What one unit does")).toBeTruthy();
    expect(screen.getByText("51")).toBeTruthy(); // estimated correction
    expect(screen.getByText("24")).toBeTruthy(); // measured correction median
    expect(screen.getByText(/median drop across 8 real correction doses/i)).toBeTruthy();
    expect(apiMocks.invoke).toHaveBeenCalledWith(
      "insulin",
      { action: "absorption", include_events: false },
    );
  });

  it("moves method, counts, and limits into the collapsible details", async () => {
    const { container } = render(<Insulin />);
    await screen.findByText("How much you use");

    const details = container.querySelector("details");
    expect(details).toBeTruthy();
    expect(details.textContent).toMatch(/How these numbers are computed/);
    // Pipeline counts and caveats live inside details, not in the main flow.
    expect(details.textContent).toMatch(/12 candidate windows/);
    expect(details.textContent).toMatch(/carbohydrate in response window \(3\)/i);
    expect(details.textContent).toMatch(/does not establish insulin causation, resistance, or absorption/i);
    expect(details.textContent).toMatch(/not pump-reported IOB/i);
    expect(details.textContent).toMatch(/does not diagnose biologic insulin resistance/i);
    const outsideDetails = container.textContent.replace(details.textContent, "");
    expect(outsideDetails).not.toMatch(/does not establish insulin causation/i);
    expect(outsideDetails).not.toMatch(/algorithm insulin-response/i);
  });

  it("renders the weekly resistance and delivery trend", async () => {
    render(<Insulin />);
    await screen.findByText("How much you use");
    expect(screen.getByText(/Over time/)).toBeTruthy();
    expect(screen.getByText(/Glucose drop per correction unit/)).toBeTruthy();
    expect(screen.getByText(/Insulin delivered \(U\/day, basal \+ bolus\)/)).toBeTruthy();
    expect(screen.getByText(/Corrections per week, by method/)).toBeTruthy();
    expect(screen.getByText(/36 corrections measured/)).toBeTruthy();
    expect(screen.getByText(/weekly since 8\/30/)).toBeTruthy();
    // Corrections available -> the weight-only proxy steps aside.
    expect(screen.queryByText(/Insulin per kg \(resistance proxy\)/)).toBeNull();
  });

  it("falls back to the per-kg proxy when there are no measured corrections", async () => {
    apiMocks.invoke.mockImplementation(async (_name, body) => {
      const base = await invokeImplementation(_name, body);
      if (body.action === "resistance") { base.data.correction_series = []; base.data.correction_summary = {}; }
      return base;
    });
    render(<Insulin />);
    await screen.findByText("How much you use");
    expect(screen.getByText(/Insulin per kg \(resistance proxy\)/)).toBeTruthy();
    expect(screen.queryByText(/Corrections per week/)).toBeNull();
  });

  it("hides the trend when under three weekly points have a per-kg value", async () => {
    apiMocks.invoke.mockImplementation(async (_name, body) => {
      const base = await invokeImplementation(_name, body);
      if (body.action === "resistance") {
        base.data.correction_series = [];
        base.data.series = [
          { date: "2026-09-13", tdd_per_kg: 0.47, avg_tdd: 39.2, avg_basal: 25.8, avg_bolus: 13.4, days: 7 },
          { date: "2026-09-20", tdd_per_kg: null, avg_tdd: 39.0, avg_basal: 26.0, avg_bolus: 13.0, days: 7 },
        ];
      }
      return base;
    });
    render(<Insulin />);
    await screen.findByText("How much you use");
    expect(screen.queryByText(/Insulin per kg \(resistance proxy\)/)).toBeNull();
  });

  it("shows measured-response strata as a pattern card", async () => {
    render(<Insulin />);
    expect(await screen.findByText(/Measured correction drop by time of day/i)).toBeTruthy();
    expect(screen.getByText(/23 mg\/dL\/U · n=4/i)).toBeTruthy();
  });

  it("keeps measured response visible when complete TDD is unavailable", async () => {
    apiMocks.invoke.mockImplementation(async (_name, body) => {
      if (body.action === "resistance") {
        return { data: { available: false, reason: "No complete TDD.", quality: {} } };
      }
      return invokeImplementation(_name, body);
    });

    const { container } = render(<Insulin />);

    expect(await screen.findByText("No complete TDD.")).toBeTruthy();
    expect(container.querySelector("details").textContent).toMatch(/12 candidate windows/);
  });
});
