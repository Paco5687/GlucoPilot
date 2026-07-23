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
        quality: {},
        reconciliation: {},
        per_phase_tdd_per_kg: {},
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
vi.mock("@/components/ContradictionPanel", () => ({ default: () => null }));

beforeEach(() => {
  apiMocks.invoke.mockImplementation(invokeImplementation);
});

afterEach(() => {
  cleanup();
  apiMocks.invoke.mockReset();
});

describe("Insulin response events", () => {
  it("shows clean/default counts, explicit reasons, strata, and noncausal assumptions", async () => {
    render(<Insulin />);

    expect(await screen.findByText("Observed insulin response events")).toBeTruthy();
    expect(screen.getByText("12")).toBeTruthy();
    expect(screen.getByText("8")).toBeTruthy();
    expect(screen.getByText(/carbohydrate in response window \(3\)/i)).toBeTruthy();
    expect(screen.getByText(/23 mg\/dL\/U · n=4/i)).toBeTruthy();
    expect(screen.getByText(/does not establish insulin causation, resistance, or absorption/i)).toBeTruthy();
    expect(screen.getByText(/not pump-reported IOB/i)).toBeTruthy();
    expect(screen.getByText(/does not diagnose biologic insulin resistance/i)).toBeTruthy();
    expect(apiMocks.invoke).toHaveBeenCalledWith(
      "insulin",
      { action: "absorption", include_events: false },
    );
  });

  it("keeps response events visible when complete TDD is unavailable", async () => {
    apiMocks.invoke.mockImplementation(async (_name, body) => {
      if (body.action === "resistance") {
        return { data: { available: false, reason: "No complete TDD.", quality: {} } };
      }
      return invokeImplementation(_name, body);
    });

    render(<Insulin />);

    expect(await screen.findByText("No complete TDD.")).toBeTruthy();
    expect(screen.getByText("Observed insulin response events")).toBeTruthy();
    expect(screen.getByText("12")).toBeTruthy();
  });
});
