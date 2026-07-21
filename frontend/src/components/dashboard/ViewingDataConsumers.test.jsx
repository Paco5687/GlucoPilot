import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GlucoseHeartRateOverlay from "./GlucoseHeartRateOverlay";
import Wearables from "../../pages/Wearables";

const apiMocks = vi.hoisted(() => ({
  glucose: vi.fn(),
  heartRate: vi.fn(),
  fitbitDaily: vi.fn(),
}));

vi.mock("@/api/base44Client", () => ({
  base44: {
    entities: {
      GlucoseReading: { filter: apiMocks.glucose },
      FitbitHeartRate: { filter: apiMocks.heartRate },
      FitbitDaily: { filter: apiMocks.fitbitDaily },
    },
    functions: { invoke: vi.fn() },
  },
}));

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

    expect(apiMocks.fitbitDaily).toHaveBeenCalledTimes(1);

    view.rerender(<Wearables />);
    view.rerender(<Wearables />);
    await settle();

    expect(apiMocks.fitbitDaily).toHaveBeenCalledTimes(1);
  });
});
