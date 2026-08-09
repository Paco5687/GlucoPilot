import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ManagementBurdenCard from "./ManagementBurdenCard";

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const payload = {
  can_edit: true,
  summary: {
    measured_effort_index: 70,
    average_active_management_minutes_per_day: 48,
    measured_interactions_per_day: 6.5,
  },
  outcomes: { time_in_range_pct: 82 },
  analytics_confidence: { confidence_score: 0.52, confidence_label: "low" },
  source_coverage: { missing: ["ketones"], available: ["pump_treatments"] },
  outcome_vs_effort: {
    sustainability_review_flag: true,
    language: "Target-range outcomes coexist with high measured effort; sustainability may deserve review.",
  },
  components: [{
    category: "bolus",
    events: 20,
    minutes: 40,
    weighted_points: 20,
    weight: 1,
  }],
  events: [{
    id: "burden-1",
    original_event_id: "burden-1",
    category: "bolus",
    duration_minutes: 2,
    origin_kind: "observed",
    effective: true,
    corrected_by: null,
    notes: "",
  }],
};

describe("ManagementBurdenCard", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async (path) => ({
      ok: true,
      json: async () => (
        String(path).startsWith("/api/management-burden?")
          ? payload
          : { id: "created" }
      ),
    })));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("separates outcomes, effort, visible components, and missing-source confidence", async () => {
    const view = render(<ManagementBurdenCard days={90} />);

    expect(await screen.findByText("Management effort")).toBeTruthy();
    expect(screen.getByText("82%")).toBeTruthy();
    expect(screen.getByText("70")).toBeTruthy();
    expect(screen.getByText(/missing sources lower confidence/i)).toBeTruthy();
    expect(screen.getByText(/ketones/i)).toBeTruthy();
    expect(screen.getByText(/sustainability may deserve review/i)).toBeTruthy();
    expect(screen.getByText(/20 events · 40 min/i)).toBeTruthy();

    view.rerender(<ManagementBurdenCard days={90} />);
    view.rerender(<ManagementBurdenCard days={90} />);
    await waitFor(() => {
      expect(
        fetch.mock.calls.filter(([path]) => String(path).startsWith("/api/management-burden?")),
      ).toHaveLength(1);
    });
  });

  it("records a manual event and explicitly refreshes", async () => {
    render(<ManagementBurdenCard days={30} />);
    await screen.findByText("Management effort");

    fireEvent.click(screen.getByRole("button", { name: "Add event" }));
    fireEvent.change(screen.getByLabelText("Effort category"), {
      target: { value: "ketone" },
    });
    fireEvent.change(screen.getByLabelText("Active management minutes"), {
      target: { value: "7" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Record" }));

    await waitFor(() => {
      const post = fetch.mock.calls.find(
        ([path, options]) => (
          path === "/api/management-burden/events" && options?.method === "POST"
        ),
      );
      expect(JSON.parse(post[1].body)).toMatchObject({
        category: "ketone",
        duration_minutes: 7,
        interaction_count: 1,
      });
    });
    expect(
      fetch.mock.calls.filter(([path]) => String(path).startsWith("/api/management-burden?")),
    ).toHaveLength(2);
  });
});
