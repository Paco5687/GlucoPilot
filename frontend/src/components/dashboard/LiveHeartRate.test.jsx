import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import LiveHeartRate from "./LiveHeartRate";

const apiMocks = vi.hoisted(() => ({
  filter: vi.fn(),
  invoke: vi.fn(),
}));

vi.mock("@/api/base44Client", () => ({
  base44: {
    entities: {
      FitbitHeartRate: { filter: apiMocks.filter },
    },
    functions: { invoke: apiMocks.invoke },
  },
}));

async function settle() {
  await act(async () => {
    await Promise.resolve();
  });
}

describe("LiveHeartRate polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-21T12:00:00Z"));
    apiMocks.filter.mockResolvedValue([]);
    apiMocks.invoke.mockResolvedValue({ data: {} });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it("queries once on mount, ignores rerenders, polls at 60 seconds, and stops on unmount", async () => {
    const view = render(
      <StrictMode>
        <LiveHeartRate />
      </StrictMode>
    );
    await settle();

    expect(apiMocks.filter).toHaveBeenCalledTimes(1);
    expect(apiMocks.filter).toHaveBeenLastCalledWith(
      { timestamp: { $gte: "2026-07-21T10:30:00.000Z" } },
      "-timestamp",
      400
    );

    for (let i = 0; i < 4; i += 1) {
      view.rerender(
        <StrictMode>
          <LiveHeartRate />
        </StrictMode>
      );
    }
    await settle();
    expect(apiMocks.filter).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(59_000);
    });
    expect(apiMocks.filter).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(apiMocks.filter).toHaveBeenCalledTimes(2);

    view.unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(180_000);
    });
    expect(apiMocks.filter).toHaveBeenCalledTimes(2);
  });

  it("preserves the explicit Refresh action", async () => {
    render(<LiveHeartRate />);
    await settle();

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await settle();

    expect(apiMocks.invoke).toHaveBeenCalledWith("googleHealth", {
      action: "sync_hr",
      minutes: 90,
    });
    expect(apiMocks.filter).toHaveBeenCalledTimes(2);
  });

  it("does not overlap a slow entity request", async () => {
    let resolveInitial;
    apiMocks.filter.mockImplementationOnce(
      () => new Promise((resolve) => {
        resolveInitial = resolve;
      })
    );

    render(<LiveHeartRate />);
    expect(apiMocks.filter).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(apiMocks.filter).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveInitial([]);
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(apiMocks.filter).toHaveBeenCalledTimes(2);
  });
});
