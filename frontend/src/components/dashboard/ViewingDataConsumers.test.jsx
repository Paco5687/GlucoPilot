import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GlucoseHeartRateOverlay from "./GlucoseHeartRateOverlay";
import Wearables from "../../pages/Wearables";

const apiMocks = vi.hoisted(() => ({
  glucose: vi.fn(),
  heartRate: vi.fn(),
  fitbitDaily: vi.fn(),
  ouraDaily: vi.fn(),
  invoke: vi.fn(),
}));

vi.mock("@/api/base44Client", () => ({
  base44: {
    entities: {
      GlucoseReading: { filter: apiMocks.glucose },
      FitbitHeartRate: { filter: apiMocks.heartRate },
      FitbitDaily: { filter: apiMocks.fitbitDaily },
      OuraDaily: { filter: apiMocks.ouraDaily },
    },
    functions: { invoke: apiMocks.invoke },
  },
}));

// recharts' ResponsiveContainer observes its box; jsdom ships no ResizeObserver,
// so any test that renders a chart with actual data needs this stub.
globalThis.ResizeObserver ||= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

async function settle() {
  await act(async () => {
    await Promise.resolve();
  });
}

describe("other useViewingData consumers", () => {
  beforeEach(() => {
    apiMocks.glucose.mockResolvedValue([]);
    apiMocks.heartRate.mockResolvedValue([]);
    apiMocks.fitbitDaily.mockResolvedValue([]);
    apiMocks.ouraDaily.mockResolvedValue([]);
    // Components fire-and-forget this one with `.catch()`, so it has to be a
    // real promise rather than a bare vi.fn() returning undefined.
    apiMocks.invoke.mockResolvedValue({ data: {} });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("does not refetch the glucose/heart-rate overlay on ordinary rerenders", async () => {
    const view = render(<GlucoseHeartRateOverlay />);
    await settle();

    expect(apiMocks.glucose).toHaveBeenCalledTimes(1);
    expect(apiMocks.heartRate).toHaveBeenCalledTimes(1);

    view.rerender(<GlucoseHeartRateOverlay />);
    view.rerender(<GlucoseHeartRateOverlay />);
    await settle();

    expect(apiMocks.glucose).toHaveBeenCalledTimes(1);
    expect(apiMocks.heartRate).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "24h" }));
    await settle();

    expect(apiMocks.glucose).toHaveBeenCalledTimes(2);
    expect(apiMocks.heartRate).toHaveBeenCalledTimes(2);
  });

  it("does not refetch Wearables on ordinary rerenders", async () => {
    const view = render(<Wearables />);
    await settle();

    // Wearables reads both rings — Google Health for most metrics, Oura for the
    // second HRV series — and neither may refetch on a plain rerender.
    expect(apiMocks.fitbitDaily).toHaveBeenCalledTimes(1);
    expect(apiMocks.ouraDaily).toHaveBeenCalledTimes(1);

    view.rerender(<Wearables />);
    view.rerender(<Wearables />);
    await settle();

    expect(apiMocks.fitbitDaily).toHaveBeenCalledTimes(1);
    expect(apiMocks.ouraDaily).toHaveBeenCalledTimes(1);
  });

  it("charts each ring's HRV as its own series without merging them", async () => {
    // Same night, two baselines: Oura reads higher than Google Health. The card
    // must keep them apart rather than averaging or overwriting.
    apiMocks.fitbitDaily.mockResolvedValue([{ date: "2026-07-22", hrv: 24 }]);
    apiMocks.ouraDaily.mockResolvedValue([{ date: "2026-07-22", hrv: 26 }]);

    render(<Wearables />);
    await settle();

    expect(screen.getByText("Heart Rate Variability")).toBeTruthy();
    // The headline number stays on the primary (Google Health) series.
    expect(screen.getByText("24.0")).toBeTruthy();
  });

  it("still charts Oura HRV on days Google Health never reported", async () => {
    apiMocks.fitbitDaily.mockResolvedValue([]);
    apiMocks.ouraDaily.mockResolvedValue([{ date: "2026-07-22", hrv: 26 }]);

    render(<Wearables />);
    await settle();

    expect(screen.getByText("Heart Rate Variability")).toBeTruthy();
  });
});
