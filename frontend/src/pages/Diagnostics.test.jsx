import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Diagnostics from "./Diagnostics";

const response = {
  contract_version: "platform-diagnostics/1.0.0",
  generated_at: "2026-07-23T15:00:00Z",
  status: "critical",
  sources: [
    {
      source: "dexcom",
      label: "Dexcom",
      configured: true,
      tracking: "governed",
      status: "current",
      last_successful_sync_at: "2026-07-23T14:00:00Z",
      data_through: "2026-07-23T13:55:00Z",
      freshness_days: 0.05,
      import_lag_seconds: 300,
      issues: [],
    },
    {
      source: "google_health",
      label: "Google Health",
      configured: true,
      tracking: "governed",
      status: "error",
      last_successful_sync_at: null,
      data_through: null,
      freshness_days: null,
      import_lag_seconds: null,
      issues: [{
        code: "configured_source_has_no_data",
        message: "Google Health is configured but has no data-through time.",
      }],
    },
  ],
  quality: {
    status: "warning",
    counters: {
      sync_failed_runs: 1,
      sync_partial_runs: 0,
      sync_failed_items: 2,
      sync_duplicate_or_skipped_items: 3,
      parser_failed_runs: 0,
      parser_failed_batches: 0,
      unverified_records: 1,
      invalid_records: 0,
      unresolved_canonical_times: 0,
    },
  },
  graph: { status: "current", published_at: "2026-07-23T13:00:00Z" },
  analytics: { status: "stale", latest_generated_at: "2026-06-01T00:00:00Z" },
  storage: {
    database_bytes: 1_048_576,
    wal_bytes: 4096,
    backup: {
      status: "current",
      latest_created_at: "2026-07-22T12:00:00Z",
      age_days: 1.13,
    },
  },
};

describe("Diagnostics", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      json: async () => response,
    })));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("loads once, ignores ordinary rerenders, and refreshes only on request", async () => {
    const view = render(<Diagnostics />);

    expect(await screen.findByText("Google Health")).toBeTruthy();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/Operational diagnostics only/i)).toBeTruthy();
    expect(screen.getByText(/configured but has no data-through time/i)).toBeTruthy();

    view.rerender(<Diagnostics />);
    view.rerender(<Diagnostics />);
    expect(fetch).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  });

  it("stops an in-flight request when the page unmounts", () => {
    vi.mocked(fetch).mockImplementation(() => new Promise(() => {}));
    const view = render(<Diagnostics />);
    const signal = vi.mocked(fetch).mock.calls[0][1].signal;

    expect(signal.aborted).toBe(false);
    view.unmount();
    expect(signal.aborted).toBe(true);
  });
});
