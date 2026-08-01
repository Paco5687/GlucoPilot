import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
  source_diagnostics: [{
    source: "dexcom",
    label: "Dexcom",
    status: "stale",
    data_through: "2026-07-18T12:00:00Z",
  }],
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
    expect(screen.getByText("Source data through")).toBeTruthy();
    expect(screen.getByText("2026-07-18 · stale")).toBeTruthy();
    expect(screen.getByText(/machine-extracted lab result is explicitly qualified as unverified/i)).toBeTruthy();
    expect(screen.getByText(/1 unresolved contradiction.*1 blocking/i)).toBeTruthy();
    expect(screen.getByText("Synthetic glucose pattern")).toBeTruthy();
    expect(screen.getByRole("button", { name: /show evidence/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /open lab result source/i }).getAttribute("href"))
      .toBe("/api/evidence/sources/LabResult/lab_1");
  });

  it("collapsible mode starts collapsed and excluded from print", () => {
    const { container } = render(<EvidenceContextBlock context={context} collapsible />);

    // Header is there, body is not, and the whole section stays out of print.
    expect(screen.getByRole("button", { name: /shared evidence context/i })).toBeTruthy();
    expect(screen.queryByText("Data quality")).toBeNull();
    expect(container.firstChild.className).toContain("print:hidden");

    // Expanding reveals the body on screen without opting into print.
    fireEvent.click(screen.getByRole("button", { name: /shared evidence context/i }));
    expect(screen.getByText("Data quality")).toBeTruthy();
    expect(screen.getByText("2026-07-20")).toBeTruthy();
    expect(container.firstChild.className).toContain("print:hidden");

    fireEvent.click(screen.getByRole("button", { name: /shared evidence context/i }));
    expect(screen.queryByText("Data quality")).toBeNull();
  });

  it("include-in-print opts the section into the printout and shows the body", () => {
    const { container } = render(<EvidenceContextBlock context={context} collapsible />);

    fireEvent.click(screen.getByRole("checkbox", { name: /include in print/i }));

    // Section will print, and the body is visible so you can see what prints.
    expect(container.firstChild.className).not.toContain("print:hidden");
    expect(screen.getByText("Data quality")).toBeTruthy();

    fireEvent.click(screen.getByRole("checkbox", { name: /include in print/i }));
    expect(container.firstChild.className).toContain("print:hidden");
    expect(screen.queryByText("Data quality")).toBeNull();
  });

  it("non-collapsible usage keeps the always-open layout with no print controls", () => {
    const { container } = render(<EvidenceContextBlock context={context} />);

    expect(screen.getByText("Data quality")).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: /include in print/i })).toBeNull();
    expect(container.firstChild.className).not.toContain("print:hidden");
  });
});
