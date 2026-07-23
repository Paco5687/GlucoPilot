import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import FingerstickLogger from "./FingerstickLogger";

const apiMocks = vi.hoisted(() => ({
  invoke: vi.fn(),
}));

vi.mock("@/api/base44Client", () => ({
  base44: { functions: { invoke: apiMocks.invoke } },
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

describe("FingerstickLogger reconciliation capture", () => {
  beforeEach(() => {
    apiMocks.invoke.mockImplementation((_name, body) => {
      if (body.action === "stats") {
        return Promise.resolve({ data: { paired: 0 } });
      }
      return Promise.resolve({
        data: {
          reading: {
            value: body.value,
            cgm_value: 118,
            delta: 8,
          },
        },
      });
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("keeps the basic entry fast and sends bounded optional context with one add", async () => {
    const onAdded = vi.fn();
    render(<FingerstickLogger onAdded={onAdded} />);
    await waitFor(() => expect(apiMocks.invoke).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByPlaceholderText("e.g. 112"), { target: { value: "110" } });
    fireEvent.click(screen.getByRole("button", { name: "Add context (optional)" }));
    fireEvent.change(screen.getByLabelText("Sensor day"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Sensor site"), { target: { value: "arm" } });
    fireEvent.change(screen.getByLabelText("Activity"), { target: { value: "resting" } });
    fireEvent.change(screen.getByLabelText("Position"), { target: { value: "lying" } });
    fireEvent.change(screen.getByLabelText("Hydration"), { target: { value: "usual" } });
    fireEvent.change(screen.getByLabelText("Compression possible"), { target: { value: "yes" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Log" }));
    });

    const addCalls = apiMocks.invoke.mock.calls.filter(([, body]) => body.action === "add");
    expect(addCalls).toHaveLength(1);
    expect(addCalls[0][0]).toBe("fingerstick");
    expect(addCalls[0][1]).toMatchObject({
      action: "add",
      value: 110,
      sensor_day: "3",
      sensor_site: "arm",
      activity: "resting",
      position: "lying",
      hydration: "usual",
      compression_possible: true,
    });
    expect(onAdded).toHaveBeenCalledTimes(1);
  });
});
