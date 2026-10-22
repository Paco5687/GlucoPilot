import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  display: {
    label: "Synthetic ferritin",
    value: "6.2",
    unit: "ng/mL",
    reference: "15–150",
    flag: "Low",
    date: "2026-07-01",
    status: null,
    detail: null,
  },
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
    concerns: [{ ...evidence, concern: { kind: "abnormal_lab", reason: "Synthetic ferritin 6.2 ng/mL — Low, reference 15–150." } }],
    problems: [{
      id: "entity:Diagnosis:synthetic",
      entity_type: "Diagnosis",
      display: { label: "Synthetic condition", status: "active", date: "2010-01-01" },
      source_links: [],
    }],
    medications: [],
    allergies: [],
    weight: [],
    labs: [evidence],
    documents: [],
    symptoms_history: [],
    objective_patterns: [],
    glucose_insulin: [],
    management_burden: [],
    hypotheses: [{
      id: "hypothesis-synthetic",
      title: "Possible synthetic iron issue",
      description: "Tentative observation.",
      evidence_state: "not_evaluated",
      display_label: "Unconfirmed — not a diagnosis. Not evaluated — no supporting or opposing evidence recorded",
      status_label: "Unconfirmed",
      definitive_allowed: false,
    }],
    reassuring_evidence: [{
      id: evidence.id,
      evidence,
      reason: "Synthetic reassurance.",
      display: { label: "Synthetic ferritin", value: "6.2", unit: "ng/mL", flag: "Low", date: "2026-07-01", detail: "Synthetic reassurance." },
    }],
    opposing_evidence: [{
      id: "contradiction-synthetic",
      reason: "Synthetic totals disagree.",
      display: { label: "Unresolved blocking contradiction", value: "source pump, total units 31.5 vs source calculated, total units 12", detail: "Synthetic totals disagree." },
    }],
    contradictions: [],
    limitations: [
      { code: "missing", message: "Synthetic source is incomplete.", display: { label: "Labs", status: "missing", detail: "Synthetic source is incomplete." } },
      { code: "legacy", message: "Synthetic limitation without a display block." },
    ],
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
    expect(screen.getByText(/Not evaluated — no supporting or opposing evidence recorded/i)).toBeTruthy();
    expect(screen.getByText("Possible synthetic iron issue").className).toContain("text-muted-foreground");
    expect(screen.getByText("Unconfirmed")).toBeTruthy();
    expect(screen.getByText("Synthetic condition")).toBeTruthy();
    const links = screen.getAllByRole("link", { name: "Open source evidence" });
    expect(links[0].getAttribute("href")).toBe("/api/evidence/source/LabResult/synthetic");
    expect(screen.getByText("Which results need follow-up?")).toBeTruthy();
  });

  it("prints each result's value, unit, reference range, flag and date", async () => {
    render(<ClinicianBrief />);
    await screen.findByText("Hematology brief");

    expect(screen.getByText(/Synthetic ferritin 6\.2 ng\/mL — Low, reference 15–150\./)).toBeTruthy();
    expect(screen.getByText("Abnormal lab")).toBeTruthy();
    const labs = screen.getByRole("heading", { name: /^Labs/ }).closest("section");
    const row = within(labs).getByRole("row", { name: /Synthetic ferritin/ });
    expect(row.textContent).toContain("6.2 ng/mL");
    expect(row.textContent).toContain("15–150");
    expect(row.textContent).toContain("Low");
    expect(row.textContent).toContain("2026-07-01");
    expect(screen.queryByText("Recorded observation.")).toBeNull();
    expect(screen.queryByText("LabResult")).toBeNull();
  });

  it("labels reassuring, opposing and limitation entries with their values and reasons", async () => {
    render(<ClinicianBrief />);
    const section = (await screen.findByRole("heading", { name: "Reassuring & opposing evidence" })).closest("section");

    expect(within(section).getByText("Synthetic ferritin")).toBeTruthy();
    expect(within(section).getByText("Synthetic reassurance.")).toBeTruthy();
    expect(within(section).getByText("Unresolved blocking contradiction")).toBeTruthy();
    expect(within(section).getByText(/source pump, total units 31\.5 vs source calculated/)).toBeTruthy();
    expect(within(section).getByText("Synthetic totals disagree.")).toBeTruthy();
    const limits = screen.getByRole("heading", { name: "Contradictions & limitations" }).closest("section");
    expect(within(limits).getByText("Labs")).toBeTruthy();
    expect(within(limits).getByText("Synthetic source is incomplete.")).toBeTruthy();
    expect(within(limits).getByText("Synthetic limitation without a display block.")).toBeTruthy();
  });

  it("shows grouped abnormal labs and a range-corrected flag note", async () => {
    const corrected = {
      ...evidence,
      id: "entity:LabResult:acr",
      display: {
        ...evidence.display, label: "Urine albumin/creatinine ratio", value: "9", unit: "mg/g", reference: "<30", flag: "Normal",
        flag_note: "Source flagged High; shown as Normal from the value and its reference range.",
      },
    };
    const group = {
      id: "derived:LabResultGroup:synthetic",
      entity_type: "LabResultGroup",
      display: { label: "Abnormal lab results", value: "2 abnormal results", date: "2026-07-01" },
      data: { count: 2 },
      source_links: [],
      concern: { kind: "abnormal_labs", reason: "Synthetic ferritin 6.2 ng/mL (Low, ref 15–150, 2026-07-01)." },
    };
    fetch.mockImplementation(async (url) => (String(url) === "/api/clinical-reviews"
      ? { ok: true, json: async () => ({ reviews: [] }) }
      : { ok: true, json: async () => ({ ...brief, sections: { ...brief.sections, concerns: [group], labs: [evidence, corrected] } }) }));
    render(<ClinicianBrief />);

    expect(await screen.findByText("Abnormal labs")).toBeTruthy();
    expect(screen.getByText("Abnormal lab results")).toBeTruthy();
    expect(screen.getByText("2 abnormal results")).toBeTruthy();
    const labs = screen.getByRole("heading", { name: /^Labs/ }).closest("section");
    const row = within(labs).getByRole("row", { name: /Urine albumin/ });
    expect(row.textContent).toContain("Source flagged High");
  });

  it("lists reported-symptom concerns once, by title", async () => {
    const symptom = (title, text) => ({
      id: `entity:HistoryEntry:${title}`,
      entity_type: "HistoryEntry",
      display: { label: `Reported symptoms: ${title}`, date: "2026-07-02", status: "note", detail: text },
      source_links: [],
      evidence_strength: { status: "observed" },
      concern: { kind: "reported_symptoms", reason: `Reported symptoms: ${title} (2026-07-02). ${text}` },
    });
    const bedtime = symptom("bedtime", "Glucose rises after 9 pm for 2.5 hours. Synthetic bedtime detail.");
    const appetite = symptom("appetite", "Synthetic appetite text.");
    const recorded = [bedtime, appetite].map(({ concern: _concern, ...entry }) => entry);
    fetch.mockImplementation(async (url) => (String(url) === "/api/clinical-reviews"
      ? { ok: true, json: async () => ({ reviews: [] }) }
      : { ok: true, json: async () => ({
        ...brief,
        sections: { ...brief.sections, concerns: [bedtime, appetite], symptoms_history: recorded },
      }) }));
    render(<ClinicianBrief />);

    const concerns = (await screen.findByRole("heading", { name: "Concerns" })).closest("section");
    const rows = within(concerns).getAllByRole("listitem").map((row) => row.textContent);
    expect(rows).toEqual(["Bedtime: Glucose rises after 9 pm for 2.5 hours.", "Appetite: Synthetic appetite text."]);
    expect(within(concerns).queryByText(/Synthetic bedtime detail/)).toBeNull();
    const history = screen.getByRole("heading", { name: "Symptoms & history" }).closest("section");
    expect(within(history).getByText("Bedtime")).toBeTruthy();
    expect(within(history).getByText("Glucose rises after 9 pm for 2.5 hours. Synthetic bedtime detail.")).toBeTruthy();
    expect(within(history).queryByText("note")).toBeNull();
  });

  it("opens labs on the abnormal results and lists the rest on request", async () => {
    const normal = {
      ...evidence,
      id: "entity:LabResult:sodium",
      display: { ...evidence.display, label: "Synthetic sodium", value: "140", unit: "mmol/L", reference: "135–145", flag: "Normal", status: "Unverified extraction" },
    };
    const low = { ...evidence, display: { ...evidence.display, status: "Unverified extraction" } };
    fetch.mockImplementation(async (url) => (String(url) === "/api/clinical-reviews"
      ? { ok: true, json: async () => ({ reviews: [] }) }
      : { ok: true, json: async () => ({ ...brief, sections: { ...brief.sections, labs: [low, normal] } }) }));
    render(<ClinicianBrief />);

    const labs = (await screen.findByRole("heading", { name: /^Labs/ })).closest("section");
    expect(within(labs).getByRole("row", { name: /Synthetic ferritin/ })).toBeTruthy();
    expect(within(labs).queryByRole("row", { name: /Synthetic sodium/ })).toBeNull();
    expect(within(labs).getByText("Status of every result: unverified extraction.")).toBeTruthy();
    fireEvent.click(within(labs).getByRole("button", { name: "Show all 2" }));
    expect(within(labs).getByRole("row", { name: /Synthetic sodium/ })).toBeTruthy();
  });

  it("keeps reference evidence and the appendix off paper", async () => {
    render(<ClinicianBrief />);

    const reference = (await screen.findByRole("heading", { name: "Reassuring & opposing evidence" })).closest("section");
    expect(reference.className).toContain("print:hidden");
    const appendix = screen.getByRole("heading", { name: "Evidence appendix" }).closest("section");
    expect(appendix.className).toContain("print:hidden");
  });

  it("trims strength boilerplate from analytics text without splitting decimals", async () => {
    const pattern = {
      id: "entity:Insight:synthetic",
      entity_type: "Insight",
      display: {
        label: "Synthetic phase pattern",
        date: "2026-07-01",
        detail: "An emerging observational association was detected. Time in range averages 90.7% against 85.3%. This may be worth discussing with their healthcare team.",
      },
      source_links: [],
      evidence_strength: { status: "emerging" },
    };
    fetch.mockImplementation(async (url) => (String(url) === "/api/clinical-reviews"
      ? { ok: true, json: async () => ({ reviews: [] }) }
      : { ok: true, json: async () => ({ ...brief, sections: { ...brief.sections, objective_patterns: [pattern] } }) }));
    render(<ClinicianBrief />);

    expect(await screen.findByText("Time in range averages 90.7% against 85.3%.")).toBeTruthy();
    expect(screen.getByText("emerging signal")).toBeTruthy();
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
