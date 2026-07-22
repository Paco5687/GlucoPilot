import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import MedicalRecordReview from "./MedicalRecordReview";

const toastMocks = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("sonner", () => ({ toast: toastMocks }));

const observation = {
  id: "labobs_synthetic",
  legacy_entity_id: "lab_synthetic",
  original_name: "Glucose, Serum",
  normalized_name: "Glucose",
  original_value: "101 H",
  normalized_value: 101,
  value_kind: "numeric",
  original_unit: "mg/dL",
  normalized_unit: "mg/dL",
  original_reference_range: "70 - 99",
  reference_low: 70,
  reference_high: 99,
  original_flag: "H",
  normalized_flag: "high",
  specimen: "serum",
  original_collected_date: "05/11/2026",
  normalized_collected_date: "2026-05-11",
  category: "Metabolic",
  source_page: 2,
  extraction_location: { description: "Results table, row 3" },
  parser_confidence: 0.97,
  validation_status: "valid",
  validation_issues: [],
  verification_status: "unverified",
  history: [],
};

describe("medical record extraction review", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ enabled: true, observations: [observation], runs: [] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ ok: true, observation: { ...observation, verification_status: "approved" } }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ enabled: true, observations: [{ ...observation, verification_status: "approved" }], runs: [] }),
      }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("opens the exact source page, switches modes, and approves the extraction", async () => {
    const onChanged = vi.fn();
    render(
      <MedicalRecordReview
        record={{ id: "record_synthetic", title: "Synthetic lab report", filename: "synthetic.pdf" }}
        isAdmin
        onChanged={onChanged}
      />
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Review extraction for Synthetic lab report" }));
    });

    expect((await screen.findAllByText("Glucose")).length).toBeGreaterThan(0);
    expect(screen.getByTitle("Medical record source preview").getAttribute("src")).toBe(
      "/api/records/file/record_synthetic?inline=1#page=2"
    );
    expect(screen.getAllByText(/101/).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /original/i }));
    expect(screen.getByText("Glucose, Serum")).toBeTruthy();
    expect(screen.getAllByText(/101 H/).length).toBeGreaterThan(0);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    });
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));

    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/records/record_synthetic/extractions/labobs_synthetic/verify",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ action: "approve" }),
      })
    );
  });
});
