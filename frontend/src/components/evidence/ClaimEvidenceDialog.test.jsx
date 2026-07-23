import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ClaimEvidenceDialog from "./ClaimEvidenceDialog";

const apiMocks = vi.hoisted(() => ({ claim: vi.fn() }));

vi.mock("@/api/base44Client", () => ({
  base44: { evidence: { claim: apiMocks.claim } },
}));

const detail = {
  claim: {
    version_number: 2,
    assertion_status: "provisional",
    algorithm: { id: "glucose-pattern-analysis", version: "2.0.0" },
  },
  evidence_set: { status: "valid" },
  evidence: {
    supporting: [{
      window_id: "window_synthetic",
      entity_type: "GlucoseReading",
      observation_count: 288,
      status: "valid",
      rationale: "Glucose observations used by the pattern rule.",
      href: "/api/evidence/windows/window_synthetic",
      source_preview: [{
        entity_type: "GlucoseReading",
        entity_id: "reading_synthetic",
        href: "/api/evidence/sources/GlucoseReading/reading_synthetic",
      }],
      source_preview_truncated: true,
    }],
    opposing: [],
    limiting: [{
      kind: "analytics_uncertainty",
      missingness: { missing_rate: 0.5 },
      discovery_status: "exploratory",
    }],
  },
  lineage: [
    { claim_version_id: "claim_v2", version_number: 2, assertion_status: "provisional" },
    { claim_version_id: "claim_v1", version_number: 1, assertion_status: "superseded" },
  ],
};

describe("ClaimEvidenceDialog", () => {
  beforeEach(() => {
    apiMocks.claim.mockResolvedValue(detail);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("loads once on demand and exposes exact source observation paths", async () => {
    render(<ClaimEvidenceDialog claimType="Pattern" claimId="pattern_synthetic" />);

    expect(apiMocks.claim).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Show evidence" }));

    expect(await screen.findByText("Evidence and claim history")).toBeTruthy();
    await waitFor(() => expect(apiMocks.claim).toHaveBeenCalledTimes(1));
    expect(apiMocks.claim).toHaveBeenCalledWith("Pattern", "pattern_synthetic");
    expect(await screen.findByText("288 observations · valid")).toBeTruthy();
    expect(screen.getByText(/Missingness: 50%/)).toBeTruthy();
    expect(screen.getByText("Version 1")).toBeTruthy();

    const source = screen.getByRole("link", { name: /Open GlucoseReading/ });
    expect(source.getAttribute("href")).toBe(
      "/api/evidence/sources/GlucoseReading/reading_synthetic"
    );
    const windowLink = screen.getByRole("link", { name: /Open window/ });
    expect(windowLink.getAttribute("href")).toBe("/api/evidence/windows/window_synthetic");
  });

  it("shows a bounded failure state when claim evidence cannot load", async () => {
    apiMocks.claim.mockRejectedValue(new Error("synthetic failure"));
    render(<ClaimEvidenceDialog claimType="Insight" claimId="insight_synthetic" />);

    fireEvent.click(screen.getByRole("button", { name: "Show evidence" }));
    expect(await screen.findByText("Evidence could not be loaded.")).toBeTruthy();
    expect(apiMocks.claim).toHaveBeenCalledTimes(1);
  });
});
