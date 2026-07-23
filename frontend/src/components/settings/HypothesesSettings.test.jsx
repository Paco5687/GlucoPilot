import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import HypothesesSettings from "./HypothesesSettings";

const hypothesis = {
  id: "hyp_synthetic",
  title: "Synthetic thyroid hypothesis",
  description: "A tentative test-only description.",
  origin_kind: "algorithm",
  origin_label: "synthetic-rule/1.0",
  status: "under_review",
  confidence_score: 0.5,
  confidence_label: "medium",
  confidence_rationale: "Weighted evidence; not a diagnostic probability.",
  evidence_revision: 2,
  evidence_input_version: "sha256:synthetic",
  suggested_verification: "Discuss a confirmatory test.",
  review_at: "2026-08-01",
  decided_by: null,
  evidence: [
    {
      role: "supporting",
      source_kind: "entity",
      source_id: "lab_synthetic",
      source_version: "v2",
      summary: "Synthetic marker supports review.",
      weight: 1,
      source_link: { href: "/api/evidence/sources/LabResult/lab_synthetic" },
    },
    {
      role: "opposing",
      source_kind: "clinical_reference",
      source_id: "reference_synthetic",
      summary: "Synthetic reference supports an alternative.",
      weight: 0.5,
      source_link: {},
    },
    {
      role: "missing",
      source_kind: "missing",
      source_id: null,
      summary: "Confirmatory testing is missing.",
      weight: 0.5,
      source_link: {},
    },
  ],
  evidence_by_role: {},
};
hypothesis.evidence_by_role = {
  supporting: [hypothesis.evidence[0]],
  opposing: [hypothesis.evidence[1]],
  missing: [hypothesis.evidence[2]],
};

describe("guarded hypothesis settings", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("keeps hypotheses visually distinct and shows both evidence sides", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ hypotheses: [hypothesis], can_edit: true }),
    }));

    render(<HypothesesSettings />);

    expect(await screen.findByText("Health hypotheses")).toBeTruthy();
    expect(screen.getByText("Hypothesis · not a diagnosis")).toBeTruthy();
    expect(screen.getByText("Under review")).toBeTruthy();
    expect(screen.getByText("Synthetic marker supports review.")).toBeTruthy();
    expect(screen.getByText("Synthetic reference supports an alternative.")).toBeTruthy();
    expect(screen.getByText("Confirmatory testing is missing.")).toBeTruthy();
    expect(screen.getAllByText(/evidence balance/i).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Record clinician confirmation" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Record clinician ruling" })).toBeTruthy();
  });

  it("keeps provider sessions read-only", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ hypotheses: [hypothesis], can_edit: false }),
    }));

    render(<HypothesesSettings />);

    expect(await screen.findByText("Synthetic thyroid hypothesis")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add evidence" })).toBeNull();
    expect(screen.queryByText("Record a tentative hypothesis")).toBeNull();
  });
});
