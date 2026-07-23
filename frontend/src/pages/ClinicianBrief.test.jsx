import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ClinicianBrief from "./ClinicianBrief";

const authState = vi.hoisted(() => ({ isProvider: true, isAdmin: false }));
vi.mock("@/lib/AuthContext", () => ({
  useAuth: () => authState,
}));

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
  evidence_bundle: { id: "urn:glucopilot:evidence-bundle:" + "a".repeat(64), version: "2.5.0" },
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
    authState.isProvider = true;
    authState.isAdmin = false;
    vi.stubGlobal("fetch", vi.fn(async (url, options = {}) => {
      if (String(url) === "/api/clinical-reviews") {
        return { ok: true, json: async () => ({ reviews: [] }) };
      }
      if (String(url) === "/api/clinical-reviews/actions") {
        return { ok: true, json: async () => ({ id: "review-synthetic" }) };
      }
      return { ok: true, json: async () => brief };
    }));
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
    const briefCalls = () => vi.mocked(fetch).mock.calls.filter(
      ([url]) => String(url) === "/api/briefs/clinician"
    );

    view.rerender(<ClinicianBrief />);
    view.rerender(<ClinicianBrief />);
    expect(briefCalls()).toHaveLength(1);

    fireEvent.change(screen.getByLabelText("Brief specialty"), {
      target: { value: "hematology" },
    });
    await waitFor(() => expect(briefCalls()).toHaveLength(2));
    const body = JSON.parse(String(briefCalls()[1][1].body));
    expect(body.mode).toBe("hematology");
    expect(body.days).toBe(90);
  });

  it("records provider review through the attributable server audit API", async () => {
    render(<ClinicianBrief />);
    await screen.findByText("Hematology brief");

    fireEvent.click(screen.getAllByRole("button", { name: "Annotate or review" })[0]);
    fireEvent.change(screen.getByLabelText("Review text"), {
      target: { value: "Synthetic provider annotation." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Record review" }));

    expect(await screen.findByText(/immutable attribution/i)).toBeTruthy();
    const actionCall = vi.mocked(fetch).mock.calls.find(
      ([url]) => String(url) === "/api/clinical-reviews/actions"
    );
    expect(actionCall).toBeTruthy();
    const body = JSON.parse(String(actionCall[1].body));
    expect(body.action).toBe("annotate");
    expect(body.target_kind).toBe("evidence_item");
    expect(body.target_id).toBe("entity:LabResult:synthetic");
  });

  it("lets the owner dispute a review without exposing provider source mutation", async () => {
    authState.isProvider = false;
    authState.isAdmin = true;
    vi.stubGlobal("fetch", vi.fn(async (url) => {
      if (String(url) === "/api/clinical-reviews") {
        return {
          ok: true,
          json: async () => ({
            reviews: [{
              id: "review-synthetic",
              target_label: "Synthetic ferritin",
              target_type: "LabResult",
              provider_status: "reviewed",
              owner_status: "pending",
              current_text: "Synthetic provider note.",
              events: [{ id: "event-synthetic" }],
            }],
          }),
        };
      }
      if (String(url).includes("/owner-decision")) {
        return { ok: true, json: async () => ({ owner_status: "disputed" }) };
      }
      return { ok: true, json: async () => brief };
    }));

    render(<ClinicianBrief />);
    expect(await screen.findByText("Synthetic provider note.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Annotate or review" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Owner review reason"), {
      target: { value: "Synthetic owner dispute reason." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Dispute" }));

    expect(await screen.findByText(/clinician history retained/i)).toBeTruthy();
    const decisionCall = vi.mocked(fetch).mock.calls.find(
      ([url]) => String(url).includes("/owner-decision")
    );
    const body = JSON.parse(String(decisionCall[1].body));
    expect(body).toEqual({
      decision: "dispute",
      reason: "Synthetic owner dispute reason.",
    });
  });
});
