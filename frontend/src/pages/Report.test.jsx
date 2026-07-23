import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Report from "./Report";

const report = {
  days: 90,
  start_date: "2026-02-01",
  end_date: "2026-05-01",
  generated_at: "2026-05-01T12:00:00Z",
  glucose: {
    available: true,
    quality: {},
    avg: 121,
    gmi: 6.2,
    tir: 80,
    cv: 31,
    readings: 100,
    days: 1,
    tbr54: 1,
    tbr70: 4,
    tar180: 16,
    tar250: 3,
    agp: [],
  },
  insulin: { available: false, quality: {}, nutrition_quality: {} },
  cycle: { available: false, quality: {} },
  wellness: { oura: null, fitbit: null, quality: {} },
  labs: { available: false, categories: {}, flagged: [], verification: {} },
  conditions: [{ name: "Synthetic confirmed diagnosis", status: "active", diagnosed: "2020-01-01" }],
  hypotheses: [{
    id: "hyp_report_synthetic",
    title: "Synthetic report hypothesis",
    description: "Tentative and under review.",
    origin_kind: "algorithm",
    origin_label: "synthetic-rule/1.0",
    status: "under_review",
    confidence_score: 0.5,
    evidence_by_role: {
      supporting: [{ summary: "Synthetic supporting evidence." }],
      opposing: [{ summary: "Synthetic opposing evidence." }],
      missing: [{ summary: "Synthetic missing evidence." }],
    },
    suggested_verification: "Synthetic clinician review.",
  }],
  health_episodes: {
    semantics: "Temporal membership and co-occurrence do not establish causation.",
    episodes: [{
      id: "episode_report_synthetic",
      title: "Synthetic fatigue flare",
      start_time: "2026-04-10",
      end_time: "2026-04-12",
      status: "confirmed",
      origin_kind: "manual",
      members: [{ entity_type: "SymptomLog" }],
      confidence: { confidence_label: "not_assessed" },
    }],
    medication_exposures: [{
      id: "exposure_report_synthetic",
      medication_name: "Synthetic medicine",
      dose: "5 mg",
      formulation: "tablet",
      start_time: "2026-04-01",
      end_time: null,
      status: "confirmed",
    }],
  },
  contradictions: {
    unresolved: [{
      id: "contr_report_synthetic",
      domain: "pump_tdd",
      severity: "blocking",
      resolution_state: "unresolved",
      detection_state: "active",
      explanation: "Synthetic pump totals disagree.",
      left: { label: "Pump reported", value: 30, unit: "units/day", observed_at: "2026-04-30" },
      right: { label: "Calculated from delivery events", value: 24, unit: "units/day", observed_at: "2026-04-30" },
    }],
  },
  evidence_context: {
    contract_version: "clinical-evidence-context/1.0.0",
    bundle: { id: "urn:bundle:report", version: "2.0.0" },
    data_quality: [{ domain: "cgm", coverage_status: "complete", freshness_status: "current" }],
    data_through: [{ domain: "cgm", through: "2026-05-01" }],
    contradictions: [{ id: "contr_report_synthetic", severity: "blocking" }],
    claims: [],
    evidence_items: [],
    sources: { links: [] },
  },
  narrative: null,
};

describe("visit report contradictions", () => {
  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => report,
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("prints both sides of every unresolved conflict", async () => {
    render(<Report />);

    expect(await screen.findByText("Unresolved data contradictions")).toBeTruthy();
    expect(screen.getByText("Synthetic pump totals disagree.")).toBeTruthy();
    expect(screen.getByText("30 units/day")).toBeTruthy();
    expect(screen.getByText("24 units/day")).toBeTruthy();
    expect(screen.getByText(/No conflicting value has been selected silently/)).toBeTruthy();
    expect(screen.getByText("Shared evidence context")).toBeTruthy();
    expect(screen.getByText("2026-05-01")).toBeTruthy();
    expect(screen.getByText("Confirmed conditions & diagnoses")).toBeTruthy();
    expect(screen.getByText(/Synthetic confirmed diagnosis/)).toBeTruthy();
    expect(screen.getByText("Health hypotheses — not diagnoses")).toBeTruthy();
    expect(screen.getByText("Synthetic supporting evidence.")).toBeTruthy();
    expect(screen.getByText("Synthetic opposing evidence.")).toBeTruthy();
    expect(screen.getByText("Synthetic missing evidence.")).toBeTruthy();
    expect(screen.getByText("Health episodes & medication exposures")).toBeTruthy();
    expect(screen.getByText(/Temporal membership and co-occurrence do not establish causation/)).toBeTruthy();
    expect(screen.getByText(/Synthetic fatigue flare/)).toBeTruthy();
    expect(screen.getByText(/Synthetic medicine/)).toBeTruthy();
  });
});
