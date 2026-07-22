import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ContradictionPanel from "./ContradictionPanel";

const auth = vi.hoisted(() => ({ isAdmin: true }));
const toast = vi.hoisted(() => ({ error: vi.fn() }));
vi.mock("@/lib/AuthContext", () => ({ useAuth: () => auth }));
vi.mock("sonner", () => ({ toast }));

const blocking = {
  id: "contr_synthetic",
  domain: "glucose",
  severity: "blocking",
  explanation: "Synthetic CGM and meter values disagree.",
  detection_state: "active",
  resolution_state: "unresolved",
  left: { label: "Fingerstick meter", value: 100, unit: "mg/dL", observed_at: "2026-05-03T12:00:00Z" },
  right: { label: "Paired CGM", value: 150, unit: "mg/dL", observed_at: "2026-05-03T12:01:00Z" },
  history: [{ id: "event_synthetic", action: "detected", actor_name: "Rule engine", actor_role: "system", created_at: "2026-05-03T12:02:00Z", reason: "" }],
};

describe("contradiction resolution panel", () => {
  beforeEach(() => {
    auth.isAdmin = true;
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ enabled: true, contradictions: [blocking] }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ...blocking, resolution_state: "resolved" }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ enabled: true, contradictions: [] }) }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("shows both sides and requires a note before resolving a blocking conflict", async () => {
    render(<ContradictionPanel domains={["glucose"]} />);

    expect(await screen.findByText("Synthetic CGM and meter values disagree.")).toBeTruthy();
    expect(screen.getByText("100 mg/dL")).toBeTruthy();
    expect(screen.getByText("150 mg/dL")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Record resolution" }));
    const resolve = screen.getByRole("button", { name: "Resolve" });
    expect(resolve.disabled).toBe(true);
    fireEvent.change(screen.getByPlaceholderText("Resolution note required"), {
      target: { value: "Synthetic source review completed." },
    });
    expect(resolve.disabled).toBe(false);

    await act(async () => { fireEvent.click(resolve); });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/contradictions/contr_synthetic/resolve",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          resolution_kind: "data_corrected",
          note: "Synthetic source review completed.",
        }),
      })
    );
  });

  it("keeps provider access read-only", async () => {
    auth.isAdmin = false;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ enabled: true, contradictions: [blocking] }),
    }));
    render(<ContradictionPanel domains={["glucose"]} />);

    expect(await screen.findByText("Synthetic CGM and meter values disagree.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Record resolution" })).toBeNull();
  });
});
