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
