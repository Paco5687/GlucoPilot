import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ActivityPositionAnalysis from "./ActivityPositionAnalysis";

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const payload = {
  can_edit: true,
  counts: {
    intervals: 2,
    manual_intervals: 1,
    wearable_intervals: 1,
    overridden_intervals: 1,
  },
  intervals: [
    {
      id: "wearable-1",
      start_time: "2026-07-20T12:00:00Z",
      end_time: "2026-07-20T12:15:00Z",
      activity: "walking",
      position: "unknown",
      origin_kind: "wearable",
      effective: false,
      notes: "",
    },
    {
      id: "manual-1",
      start_time: "2026-07-20T12:00:00Z",
      end_time: "2026-07-20T12:15:00Z",
      activity: "resting",
      position: "standing",
      origin_kind: "manual",
      effective: true,
      notes: "Corrected after review",
    },
  ],
  effects: [
    {
      id: "effect-1",
      dimension: "position",
      state: "standing",
      metric: "glucose_slope_mg_dl_per_hour",
      observed_mean: 4.2,
      unit: "mg/dL/hour",
      sample_count: 14,
      measured_interval_count: 14,
      interval_count: 16,
      replication_status: "not-attempted",
      analytics_confidence: { discovery_status: "emerging" },
      language: {
        lead: "An emerging observational association was detected.",
      },
    },
  ],
};

describe("ActivityPositionAnalysis", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path) => ({
        ok: true,
        json: async () => (
          String(path).startsWith("/api/activity-position?")
            ? payload
            : { id: "created" }
        ),
      })),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("shows provenance, confidence, missingness, and noncausal language", async () => {
    render(<ActivityPositionAnalysis days={90} />);

    expect(await screen.findByText("Activity & position")).toBeTruthy();
    expect(screen.getByText(/temporal associations—not evidence/i)).toBeTruthy();
    expect(screen.getByText(/14\/16 intervals measured/i)).toBeTruthy();
    expect(screen.getByText(/replication not-attempted/i)).toBeTruthy();
    expect(screen.getByText(/wearable · overridden/i)).toBeTruthy();
    expect(screen.getByText("Corrected after review")).toBeTruthy();
  });

  it("records a manual interval and refreshes the analysis", async () => {
    render(<ActivityPositionAnalysis days={30} />);
    await screen.findByText("Activity & position");

    fireEvent.change(screen.getByLabelText("Activity state"), {
      target: { value: "walking" },
    });
    fireEvent.change(screen.getByLabelText("Position state"), {
      target: { value: "standing" },
    });
    fireEvent.change(screen.getByLabelText("Activity interval notes"), {
      target: { value: "After lunch" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Record interval" }));

    await waitFor(() => {
      const post = fetch.mock.calls.find(
        ([path, options]) => (
          path === "/api/activity-position/intervals"
          && options?.method === "POST"
        ),
      );
      expect(post).toBeTruthy();
      expect(JSON.parse(post[1].body)).toMatchObject({
        activity: "walking",
        position: "standing",
        notes: "After lunch",
      });
    });
    expect(
      fetch.mock.calls.filter(([path]) => String(path).startsWith("/api/activity-position?")),
    ).toHaveLength(2);
  });
});
