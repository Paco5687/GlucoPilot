import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import EvidenceContextBlock from "./EvidenceContextBlock";

vi.mock("@/api/base44Client", () => ({
  base44: { evidence: { claim: vi.fn() } },
}));

const context = {
  contract_version: "clinical-evidence-context/1.0.0",
  bundle: { id: "urn:bundle:synthetic", version: "2.0.0" },
  data_quality: [{
    domain: "cgm",
    coverage_status: "complete",
    freshness_status: "current",
  }],
  data_through: [{ domain: "cgm", through: "2026-07-20" }],
  contradictions: [{ id: "contr_1", severity: "blocking" }],
  claims: [{
    claim_type: "Pattern",
    claim_id: "pattern_1",
    title: "Synthetic glucose pattern",
  }],
  evidence_items: [{
    id: "entity:LabResult:lab_1",
    entity_type: "LabResult",
    confidence: { clinically_verified: false },
    source_links: [{
      kind: "normalized_entity",
      entity_type: "LabResult",
      href: "/api/evidence/sources/LabResult/lab_1",
    }],
  }],
  sources: { links: [] },
};

describe("shared evidence context", () => {
  afterEach(cleanup);

  it("shows common quality, data-through, conflict, claim, and source semantics", () => {
    render(
      <EvidenceContextBlock
        context={context}
        narrativeEvidenceIds={["entity:LabResult:lab_1"]}
      />,
    );

    expect(screen.getByText("Shared evidence context")).toBeTruthy();
    expect(screen.getByText("2026-07-20")).toBeTruthy();
    expect(screen.getByText(/machine-extracted lab result is explicitly qualified as unverified/i)).toBeTruthy();
    expect(screen.getByText(/1 unresolved contradiction.*1 blocking/i)).toBeTruthy();
    expect(screen.getByText("Synthetic glucose pattern")).toBeTruthy();
    expect(screen.getByRole("button", { name: /show evidence/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /open lab result source/i }).getAttribute("href"))
      .toBe("/api/evidence/sources/LabResult/lab_1");
  });
});
