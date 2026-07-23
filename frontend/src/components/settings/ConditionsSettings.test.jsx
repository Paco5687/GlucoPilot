import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ConditionsSettings from "./ConditionsSettings";

describe("confirmed conditions and legacy hypotheses", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("does not present a legacy suspected entry as a confirmed diagnosis", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        conditions: [
          { id: "diagnosis_synthetic", name: "Synthetic confirmed condition", status: "active" },
          { id: "suspected_synthetic", name: "Synthetic legacy suspicion", status: "suspected" },
        ],
      }),
    }));

    render(<ConditionsSettings />);

    expect(await screen.findByText("Synthetic confirmed condition")).toBeTruthy();
    expect(screen.getByText("Legacy suspected entries · hypotheses, not diagnoses")).toBeTruthy();
    expect(screen.getByText("Synthetic legacy suspicion")).toBeTruthy();
    expect(screen.queryByRole("option", { name: "suspected" })).toBeNull();
  });
});
