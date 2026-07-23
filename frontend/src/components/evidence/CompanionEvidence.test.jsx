import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import CompanionEvidence from "./CompanionEvidence";

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(),
}));

vi.mock("@/api/base44Client", () => ({
  base44: { functions: { invoke: mocks.invoke } },
}));

const evidence = {
  contract_version: "companion-evidence-context/1.0.0",
  bundle: { id: "urn:synthetic:bundle", version: "2.0.0" },
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Companion evidence commands", () => {
  it("shows classified personal claims and their source links", async () => {
    mocks.invoke.mockResolvedValue({
      data: {
        command: "show",
        bundle: evidence.bundle,
        statements: [{
          ordinal: 0,
          text: "Unverified machine-extracted lab evidence: Synthetic result was high. [E1]",
          classification: "observation",
          evidence_aliases: ["E1"],
        }],
        evidence_items: [{
          alias: "E1",
          source_links: [{
            kind: "normalized_entity",
            entity_type: "LabResult",
            href: "/api/evidence/sources/LabResult/lab_synthetic",
          }],
        }],
        external_sources: [],
        missing_data_caveats: [],
      },
    });
    render(<CompanionEvidence messageId="message_synthetic" evidence={evidence} />);

    fireEvent.click(screen.getByRole("button", { name: "Show evidence" }));

    expect(await screen.findByText("Observation")).toBeTruthy();
    expect(screen.getByText("E1")).toBeTruthy();
    expect(screen.getByText(/Unverified machine-extracted lab evidence/)).toBeTruthy();
    expect(screen.getByRole("link", { name: /Open LabResult/i }).getAttribute("href"))
      .toBe("/api/evidence/sources/LabResult/lab_synthetic");
    expect(mocks.invoke).toHaveBeenCalledWith("companion", {
      action: "evidence_command",
      command: "show",
      message_id: "message_synthetic",
    });
  });

  it("keeps both sides of opposing evidence visible", async () => {
    mocks.invoke.mockResolvedValue({
      data: {
        command: "opposing",
        opposing_evidence: [],
        contradictions: [{
          id: "contradiction_synthetic",
          explanation: "Synthetic sources disagree.",
          left: { label: "Source A", value: 7.1, unit: "mIU/L" },
          right: { label: "Source B", value: 4.2, unit: "mIU/L" },
        }],
      },
    });
    render(<CompanionEvidence messageId="message_synthetic" evidence={evidence} />);

    fireEvent.click(screen.getByRole("button", { name: "What argues against this?" }));

    expect(await screen.findByText("Synthetic sources disagree.")).toBeTruthy();
    expect(screen.getByText("Source A · 7.1 · mIU/L")).toBeTruthy();
    expect(screen.getByText("Source B · 4.2 · mIU/L")).toBeTruthy();
    expect(screen.getByText("Neither side was silently selected.")).toBeTruthy();
  });

  it("reports whether the underlying bundle changed", async () => {
    mocks.invoke.mockResolvedValue({
      data: {
        command: "changes",
        changed: true,
        changed_scopes: ["labs_records"],
        checked_at: "2026-07-23T12:00:00Z",
      },
    });
    render(<CompanionEvidence messageId="message_synthetic" evidence={evidence} />);

    fireEvent.click(screen.getByRole("button", { name: "What changed?" }));

    expect(await screen.findByText("The underlying evidence changed since this answer.")).toBeTruthy();
    expect(screen.getByText("Changed scopes: labs_records")).toBeTruthy();
  });
});
