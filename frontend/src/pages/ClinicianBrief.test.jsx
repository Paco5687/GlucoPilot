import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ClinicianBrief from "./ClinicianBrief";

const evidence = {
  id: "entity:LabResult:synthetic",
  entity_type: "LabResult",
  entity_id: "synthetic",
  title: "Synthetic ferritin",
  observed_at: "2026-07-01",
  data: {},
  evidence_strength: {
    status: "observed",
    lead: "Recorded observation.",
    definitive_allowed: false,
    causal_allowed: false,
  },
  source_links: [{
    href: "/api/evidence/source/LabResult/synthetic",
    kind: "normalized_entity",
  }],
};

const brief = {
  mode: "hematology",
  mode_label: "Hematology",
  window: { days: 90 },
  evidence_bundle: { version: "2.4.0" },
  privacy: {
    note: "Only specialty-allowlisted Evidence Bundle items are included; other PHI is omitted.",
  },
  language: {
    hypotheses: "Unconfirmed hypotheses are tentative and are not diagnoses.",
    associations: "Observed associations and calculations do not establish causation.",
  },
  sections: {
    concerns: [evidence],
    objective_patterns: [],
    glucose_insulin: [],
    management_burden: [],
    labs_imaging: [evidence],
    hypotheses: [{
      id: "hypothesis-synthetic",
      title: "Possible synthetic iron issue",
      description: "Tentative observation.",
      display_label: "Unconfirmed hypothesis — not a diagnosis",
      definitive_allowed: false,
    }],
    reassuring_evidence: [{ evidence, reason: "Synthetic reassurance." }],
    opposing_evidence: [],
    contradictions: [],
    limitations: [{ code: "missing", message: "Synthetic source is incomplete." }],
    questions: ["Which results need follow-up?"],
  },
  appendix: [evidence],
};

describe("ClinicianBrief", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      json: async () => brief,
    })));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("shows specialty minimization, tentative hypotheses, strength, and source links", async () => {
    render(<ClinicianBrief />);

    expect(await screen.findByText("Hematology brief")).toBeTruthy();
    expect(screen.getByText(/other PHI is omitted/i)).toBeTruthy();
    expect(screen.getAllByText(/Unconfirmed hypothesis/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Recorded observation/).length).toBeGreaterThan(0);
    const links = screen.getAllByRole("link", { name: "Open source evidence" });
    expect(links[0].getAttribute("href")).toBe("/api/evidence/source/LabResult/synthetic");
    expect(screen.getByText("Which results need follow-up?")).toBeTruthy();
  });

  it("regenerates only when specialty or range meaningfully changes", async () => {
    const view = render(<ClinicianBrief />);
    await screen.findByText("Hematology brief");

    view.rerender(<ClinicianBrief />);
    view.rerender(<ClinicianBrief />);
    expect(fetch).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText("Brief specialty"), {
      target: { value: "hematology" },
    });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    const body = JSON.parse(String(vi.mocked(fetch).mock.calls[1][1].body));
    expect(body.mode).toBe("hematology");
    expect(body.days).toBe(90);
  });
});
