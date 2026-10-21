import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import PumpSettingsCard from "./PumpSettingsCard";

const payload = {
  available: true,
  snapshot_count: 2,
  changes: [{ snapshot_at: "2026-06-03T14:30:00Z", previous_snapshot_at: "2026-03-02T15:00:00Z", changed: ["Carb ratio"] }],
  latest: {
    snapshot_at: "2026-06-03T14:30:00Z",
    as_of: "2026-06-03",
    age_days: 139,
    device_model: null,
    schedules: [
      { key: "carb_ratio_segments", label: "Carb ratio", unit: "g/U", segments: [
        { start: "00:00", end: "06:00", value: 12 },
        { start: "06:00", end: "24:00", value: 10 },
      ] },
      { key: "isf_segments", label: "Correction factor (ISF)", unit: "mg/dL/U", segments: [{ start: "00:00", end: "24:00", value: 45 }] },
    ],
    scalars: [{ key: "active_insulin_time_hours", label: "Active insulin time", unit: "h", value: 3 }],
    basal_programs: [{ name: "Weekday", active: true, units_per_day: 18, segments: [{ start: "00:00", end: "24:00", value: 0.75 }] }],
  },
};

function mockFetch(body, ok = true) {
  globalThis.fetch = vi.fn(async () => ({ ok, json: async () => body }));
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("PumpSettingsCard", () => {
  it("leads with the snapshot's age and shows segment tables", async () => {
    mockFetch(payload);
    render(<PumpSettingsCard />);
    await waitFor(() => expect(screen.getByText("Pump settings")).toBeTruthy());
    expect(screen.getByText(/139 days old/)).toBeTruthy();
    expect(screen.getByText("Carb ratio")).toBeTruthy();
    expect(screen.getByText("06:00–24:00")).toBeTruthy();
    expect(screen.getByText(/Basal: Weekday \(active\)/)).toBeTruthy();
    expect(screen.getByText(/: Carb ratio$/)).toBeTruthy();
  });

  it("renders nothing without a snapshot", async () => {
    mockFetch({ available: false, latest: null });
    const { container } = render(<PumpSettingsCard />);
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    expect(container.innerHTML).toBe("");
  });
});
