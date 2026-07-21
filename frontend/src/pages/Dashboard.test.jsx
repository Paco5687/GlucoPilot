import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Dashboard from "./Dashboard";

const apiMocks = vi.hoisted(() => ({
  filters: new Map(),
  invoke: vi.fn(),
}));

vi.mock("@/api/base44Client", () => ({
  base44: {
    entities: new Proxy({}, {
      get(_target, name) {
        if (!apiMocks.filters.has(name)) apiMocks.filters.set(name, vi.fn().mockResolvedValue([]));
        return { filter: apiMocks.filters.get(name) };
      },
    }),
    functions: { invoke: apiMocks.invoke },
  },
}));

vi.mock("../components/dashboard/CurrentGlucose", () => ({ default: () => null }));
vi.mock("../components/dashboard/MetricsCards", () => ({ default: () => null }));
vi.mock("../components/dashboard/GlucoseChart", () => ({ default: () => null }));
vi.mock("../components/dashboard/TreatmentTimeline", () => ({ default: () => null }));
vi.mock("../components/dashboard/TreatmentSummaryCards", () => ({ default: () => null }));
vi.mock("../components/dashboard/DailyTIRChart", () => ({ default: () => null }));
vi.mock("../components/dashboard/DailyAverageChart", () => ({ default: () => null }));
vi.mock("../components/dashboard/HeatmapChart", () => ({ default: () => null }));
vi.mock("../components/dashboard/DayOfWeekChart", () => ({ default: () => null }));
vi.mock("../components/dashboard/OuraPanel", () => ({ default: () => null }));
vi.mock("../components/dashboard/WearablesPanel", () => ({ default: () => null }));
vi.mock("../components/dashboard/LiveHeartRate", () => ({ default: () => null }));
vi.mock("../components/dashboard/FingerstickLogger", () => ({ default: () => null }));
vi.mock("../components/dashboard/SymptomNudge", () => ({ default: () => null }));
vi.mock("../components/dashboard/GlucoseOuraOverlay", () => ({ default: () => null }));
vi.mock("../components/dashboard/CorrelationCards", () => ({ default: () => null }));
vi.mock("../components/dashboard/TimeRangePicker", () => ({
  default: () => null,
  RANGES: [{ key: "3h", hours: 3 }],
}));

async function settle() {
  await act(async () => {
    await Promise.resolve();
  });
}

describe("Dashboard polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    apiMocks.filters.clear();
    apiMocks.invoke.mockResolvedValue({ data: { readings: [] } });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it("uses one 60-second refresh timer and clears it on unmount", async () => {
    const view = render(<Dashboard />);
    await settle();

    const glucose = apiMocks.filters.get("GlucoseReading");
    const treatments = apiMocks.filters.get("Treatment");
    expect(glucose).toHaveBeenCalledTimes(1);
    expect(treatments).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(59_000);
    });
    expect(glucose).toHaveBeenCalledTimes(1);
    expect(treatments).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(glucose).toHaveBeenCalledTimes(2);
    expect(treatments).toHaveBeenCalledTimes(2);

    view.unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    expect(glucose).toHaveBeenCalledTimes(2);
    expect(treatments).toHaveBeenCalledTimes(2);
  });
});
