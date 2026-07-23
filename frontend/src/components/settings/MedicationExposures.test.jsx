import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import MedicationExposures from "./MedicationExposures";

const exposure = {
  id: "exposure_synthetic",
  medication_name: "Synthetic medicine",
  dose: "5 mg",
  formulation: "tablet",
  frequency: "daily",
  start_time: "2026-07-01",
  end_time: null,
  origin_kind: "manual",
  status: "proposed",
};

function response(data) {
  return Promise.resolve({ ok: true, json: async () => data });
}

describe("medication exposure intervals", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("shows an ongoing interval and creates a proposed bounded interval", async () => {
    const fetchMock = vi.fn((path, options = {}) => {
      if (options.method === "POST") {
        return response({ ...exposure, id: "exposure_created", medication_name: "New medicine" });
      }
      return response({ medication_exposures: [exposure], can_edit: true });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MedicationExposures />);
    expect(await screen.findByText("Synthetic medicine")).toBeTruthy();
    expect(screen.getByText(/2026-07-01 → ongoing/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Exposure medication name"), {
      target: { value: "New medicine" },
    });
    fireEvent.change(screen.getByLabelText("Exposure end"), {
      target: { value: "2026-07-20" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add interval" }));

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([path, options]) => (
        path === "/api/medication-exposures" && options?.method === "POST"
      ))).toBe(true);
    });
    const createCall = fetchMock.mock.calls.find(([path, options]) => (
      path === "/api/medication-exposures" && options?.method === "POST"
    ));
    const body = JSON.parse(createCall[1].body);
    expect(body.medication_name).toBe("New medicine");
    expect(body.end_time).toBe("2026-07-20");
    expect(body.origin_kind).toBe("manual");
  });

  it("keeps provider sessions read-only", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response({
      medication_exposures: [exposure],
      can_edit: false,
    })));
    render(<MedicationExposures />);

    expect(await screen.findByText("Synthetic medicine")).toBeTruthy();
    expect(screen.queryByLabelText("Exposure medication name")).toBeNull();
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
  });
});
