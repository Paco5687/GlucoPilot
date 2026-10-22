import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthContext } from "@/lib/AuthContext";
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
    fingerstick_reconciliation: {
      paired: 6,
      mean_abs_delta: 12.5,
      semantics: "CGM and meter values remain separate observations.",
      persistent_bias: {
        classification: "cgm_high",
        sample_count: 6,
        minimum_sample_count: 5,
      },
      low_reconciliation: {
        confirmed_low: 2,
        cgm_only_low: 1,
        caveat: "These counts describe only meter-checked moments and do not correct CGM time-below-range.",
      },
    },
  },
  insulin: { available: false, quality: {}, nutrition_quality: {} },
  insulin_response: {
    available: false,
    reason: "Only 2 clean correction boluses in 120d — need 8.",
    algorithm_version: "insulin-response/1.0.0",
    window_days: 120,
    counts: { total: 4, clean: 2, confounded: 1, excluded: 1 },
    reason_counts: { carbohydrate_in_response_window: 1 },
    quality: {},
  },
  activity_position: {
    effects: [{
      id: "activity-effect-report",
      dimension: "position",
      state: "standing",
      metric: "glucose_slope_mg_dl_per_hour",
      observed_mean: 3.2,
      unit: "mg/dL/hour",
      sample_count: 14,
      measured_interval_count: 14,
      interval_count: 16,
      replication_status: "not-attempted",
      analytics_confidence: { discovery_status: "emerging" },
    }],
  },
  management_burden: {
    summary: {
      measured_effort_index: 70,
      average_active_management_minutes_per_day: 48,
    },
    outcomes: { time_in_range_pct: 82 },
    analytics_confidence: { confidence_score: 0.52, confidence_label: "low" },
    source_coverage: { missing: ["ketones"] },
    outcome_vs_effort: {
      sustainability_review_flag: true,
      language: "Target-range outcomes coexist with high measured effort; sustainability may deserve review.",
    },
    components: [{
      category: "bolus",
      events: 20,
      minutes: 40,
      weighted_points: 20,
      weight: 1,
    }],
  },
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
    expect(screen.getByText("Observed insulin response events")).toBeTruthy();
    expect(screen.getByText(/carbohydrate in response window \(1\)/i)).toBeTruthy();
    expect(screen.getByText(/does not establish insulin causation, resistance, or absorption/i)).toBeTruthy();
    expect(screen.getByText("30 units/day")).toBeTruthy();
    expect(screen.getByText("24 units/day")).toBeTruthy();
    expect(screen.getByText(/No conflicting value has been selected silently/)).toBeTruthy();
    // The evidence context is reference material: collapsed until expanded.
    const evidenceToggle = screen.getByRole("button", { name: /shared evidence context/i });
    expect(screen.queryByText("2026-05-01")).toBeNull();
    fireEvent.click(evidenceToggle);
    expect(screen.getByText("2026-05-01")).toBeTruthy();
    expect(screen.getByText("Confirmed diagnoses")).toBeTruthy();
    expect(screen.getByText(/Synthetic confirmed diagnosis/)).toBeTruthy();
    expect(screen.getByText("Questions under review")).toBeTruthy();
    expect(screen.getByText("Mixed evidence")).toBeTruthy();
    expect(screen.queryByText(/evidence balance/i)).toBeNull();
    expect(screen.getByText("Synthetic supporting evidence.")).toBeTruthy();
    expect(screen.getByText("Synthetic opposing evidence.")).toBeTruthy();
    expect(screen.getByText("Synthetic missing evidence.")).toBeTruthy();
    expect(screen.getByText("Health episodes & medication exposures")).toBeTruthy();
    expect(screen.getByText(/Temporal membership and co-occurrence do not establish causation/)).toBeTruthy();
    expect(screen.getByText(/Synthetic fatigue flare/)).toBeTruthy();
    expect(screen.getByText(/Synthetic medicine/)).toBeTruthy();
    expect(screen.getByText("CGM and meter reconciliation")).toBeTruthy();
    expect(screen.getByText(/persistent CGM-high direction observed/)).toBeTruthy();
    expect(screen.getByText(/do not correct CGM time-below-range/)).toBeTruthy();
    expect(screen.getByText("Activity & position associations")).toBeTruthy();
    expect(screen.getByText(/14\/16 intervals measured/)).toBeTruthy();
    expect(screen.getByText(/do not establish that activity or position caused/i)).toBeTruthy();
    expect(screen.getByText("Outcomes & management effort")).toBeTruthy();
    expect(screen.getByText(/missing event sources reduce confidence/i)).toBeTruthy();
    expect(screen.getByText(/sustainability may deserve review/i)).toBeTruthy();
  });

  it("keeps unsupported legacy suspicions collapsed and out of print by default", async () => {
    const reportWithLegacyQuestion = {
      ...report,
      hypotheses: [{
        id: "legacy_synthetic",
        title: "Synthetic legacy question",
        legacy: true,
        status: "proposed",
        evidence_state: "not_evaluated",
        evidence_by_role: {
          supporting: [],
          opposing: [],
          missing: [{ summary: "Clinician review needed." }],
        },
        suggested_verification: "Discuss whether this should be evaluated.",
      }],
    };
    vi.mocked(fetch).mockResolvedValueOnce(/** @type {Response} */ ({
      ok: true,
      json: async () => reportWithLegacyQuestion,
    }));

    render(<Report />);

    const toggle = await screen.findByRole("button", {
      name: "Questions previously recorded (1)",
    });
    expect(screen.queryByText("Synthetic legacy question")).toBeNull();
    // Both optional sections — legacy questions and the evidence context —
    // stay out of the printout until explicitly included.
    for (const checkbox of screen.getAllByLabelText("Include in print")) {
      expect(checkbox).toHaveProperty("checked", false);
    }
    expect(screen.queryByText(/evidence balance/i)).toBeNull();

    fireEvent.click(toggle);

    expect(screen.getByText("Synthetic legacy question")).toBeTruthy();
    expect(screen.getByText(/reminders for discussion, not findings/i)).toBeTruthy();
    expect(screen.getByText(/not evaluated · no supporting or opposing evidence recorded/i)).toBeTruthy();
  });
});

describe("visit report clinical framing", () => {
  const framed = {
    ...report,
    patient: { name: "Synthetic Patient", date_of_birth: "1990-02-03", age: 36, sex: "female" },
    insurance: { available: true, member_name: "Synthetic Subscriber", carrier: "Synthetic Carrier" },
    weight: {
      available: true,
      units: "imperial",
      entries: 4,
      latest: { date: "2026-10-15", weight_kg: 61.0 },
      // A years-old baseline below today's weight: change since it is a gain.
      first: { date: "2021-05-01", weight_kg: 50.0 },
      peak: { date: "2026-03-01", weight_kg: 70.0 },
      peak_change: { from_date: "2026-03-01", from_kg: 70.0, change_kg: -9.0, change_pct: -12.9, days: 228 },
      window_change: { from_date: "2026-07-20", from_kg: 64.0, change_kg: -3.0, change_pct: -4.7, days: 87 },
      since_first_change: { from_date: "2021-05-01", from_kg: 50.0, change_kg: 11.0, change_pct: 22.0, days: 1994 },
      series: [
        { date: "2021-05-01", weight_kg: 50.0 },
        { date: "2026-03-01", weight_kg: 70.0 },
        { date: "2026-07-20", weight_kg: 64.0 },
        { date: "2026-10-15", weight_kg: 61.0 },
      ],
    },
    glucose: { ...report.glucose, tir: 72, tbr70: 10.3, tbr54: 3.1, tar180: 17.7, tar250: 4 },
    insulin: {
      available: true,
      quality: {},
      nutrition_quality: {},
      pump_reported_avg_tdd: 37.9,
      pump_reported_days: 80,
      pump_reported_sources: { glooko: 80 },
      avg_tdd_est: 37.9,
      avg_daily_bolus: 0.1,
      boluses_per_day: 0.1,
      bolus_source: "Glooko bolus event stream",
      bolus_stream_incomplete: true,
      temp_basal_corrections: {
        count: 40, extra_units_total: 120.4, extra_units_per_day: 1.3, per_day: 0.4,
        source: "Glooko temporary basal (manual mode)",
      },
      scheduled_avg_daily_basal: 16.0,
      avg_daily_carbs: 4,
      carbs_representative: false,
      carb_logged_days: 9,
      carb_expected_days: 90,
    },
    labs: {
      available: true,
      flagged: [],
      key_results: [{
        test_name: "Hemoglobin A1c", value: 6.8, unit: "%", reference_high: 5.6, reference_low: null,
        flag: "high", date: "2026-04-02", collected_date: "04/02/2026", verification: { qualified: true, verification_status: "unverified" },
      }],
      categories: {
        Chemistry: [{
          test_name: "Hemoglobin A1c", reported_names: ["HbA1c"], value: 6.8, unit: "%", flag: "high",
          date: "2026-04-02", collected_date: "04/02/2026", trend: "down", verification: {},
        }],
      },
      duplicates_removed: 3,
      undated_results: 1,
      verification: {},
    },
    narrative: null,
    narrative_status: {
      available: false,
      reason: "The written summary is unavailable: the language model request failed.",
      note: "Every figure in this report is computed directly from the data and is unaffected.",
    },
  };

  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => framed }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("heads the report with the patient's name and date of birth", async () => {
    render(<Report />);

    expect(await screen.findByText("Synthetic Patient")).toBeTruthy();
    expect(screen.getByText(/DOB 1990-02-03 · 36 y · Female/)).toBeTruthy();
  });

  it("headlines weight change from the 12-month high, not from a years-old first entry", async () => {
    render(<Report />);

    expect(await screen.findByText("Weight")).toBeTruthy();
    expect(screen.getByText("134.5 lb")).toBeTruthy();
    const fromPeak = screen.getByText("Change from 12-month high").parentElement;
    expect(fromPeak.textContent).toContain("−19.8 lb (−12.9%)");
    expect(fromPeak.textContent).toContain("from 154.3 lb on 2026-03-01 · 228 days");
    expect(fromPeak.querySelector(".text-red-600")).toBeTruthy();
    expect(screen.getByText("−6.6 lb (−4.7%)")).toBeTruthy();
    // The long-run baseline is secondary context, never a headline tile.
    expect(screen.queryByText("Change since first entry")).toBeNull();
    expect(screen.getByText(/First logged weight: 110.2 lb on 2021-05-01 \(\+24.3 lb \(\+22.0%\) to latest\)/)).toBeTruthy();
    expect(screen.getByText(/Weight loss of 5% or more of body weight is recorded/)).toBeTruthy();
  });

  it("gives hypoglycemia its own tiles flagged against consensus targets", async () => {
    render(<Report />);

    const below70 = (await screen.findByText("Time below 70 mg/dL")).parentElement;
    expect(below70.textContent).toContain("10.3%");
    expect(below70.textContent).toContain("target <4% · above target");
    expect(below70.querySelector(".text-red-600")).toBeTruthy();
    const below54 = screen.getByText("Time below 54 mg/dL").parentElement;
    expect(below54.textContent).toContain("3.1%");
    expect(below54.textContent).toContain("target <1% · above target");
    expect(screen.getByText(/Time below range exceeds the consensus target/)).toBeTruthy();
  });

  it("marks hypoglycemia within target when it is", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(/** @type {Response} */ ({
      ok: true,
      json: async () => ({ ...framed, glucose: { ...framed.glucose, tbr70: 2.0, tbr54: 0.4 } }),
    }));
    render(<Report />);

    const below70 = (await screen.findByText("Time below 70 mg/dL")).parentElement;
    expect(below70.textContent).toContain("within target");
    expect(below70.querySelector(".text-green-600")).toBeTruthy();
    expect(screen.queryByText(/Time below range exceeds the consensus target/)).toBeNull();
  });

  it("says when the narrative is unavailable instead of silently omitting it", async () => {
    render(<Report />);

    expect(await screen.findByText("Narrative unavailable")).toBeTruthy();
    expect(screen.getByText(/language model request failed/)).toBeTruthy();
    expect(screen.queryByText("Quarter in review")).toBeNull();
  });

  it("puts key lab results with flags above the full tables", async () => {
    render(<Report />);

    const panel = (await screen.findByText(/Key results · most recent value/)).parentElement;
    expect(panel.textContent).toContain("Hemoglobin A1c");
    expect(panel.textContent).toContain("6.8");
    expect(panel.textContent).toContain("high");
    expect(panel.textContent).toContain("2026-04-02");
    expect(screen.getByText(/also reported as HbA1c/)).toBeTruthy();
    expect(screen.getByText(/3 duplicate results \(same test, date, and value\) merged/)).toBeTruthy();
  });

  it("labels insulin sources and omits tiles that would read as non-adherence", async () => {
    render(<Report />);

    expect(await screen.findByText("Insulin & carbs")).toBeTruthy();
    expect(screen.queryByText("Bolus insulin")).toBeNull();
    expect(screen.queryByText("Carbs logged")).toBeNull();
    const corrections = screen.getByText("Temp-basal corrections").parentElement;
    expect(corrections.textContent).toContain("1.3");
    expect(corrections.textContent).toContain("above programmed rate");
    expect(screen.getByText("Programmed basal schedule")).toBeTruthy();
    expect(screen.getByText(/Bolus\/basal split not available/).textContent).toContain("not a record of missed doses");
    expect(screen.getByText(/Carbs were logged on 9 of 90 days/)).toBeTruthy();
  });

  it("states insulin per kilogram from the latest weight", async () => {
    render(<Report />);

    const tdd = (await screen.findByText("Total daily insulin")).parentElement;
    expect(tdd.textContent).toContain("37.9");
    expect(tdd.textContent).toContain("0.62 U/kg");
  });

  it("still shows bolus and carb tiles when those streams are complete", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(/** @type {Response} */ ({
      ok: true,
      json: async () => ({
        ...framed,
        insulin: {
          ...framed.insulin,
          avg_daily_bolus: 18.2,
          bolus_stream_incomplete: false,
          carbs_representative: true,
          avg_daily_carbs: 150,
        },
      }),
    }));
    render(<Report />);

    const bolus = (await screen.findByText("Bolus insulin")).parentElement;
    expect(bolus.textContent).toContain("18.2");
    expect(bolus.textContent).toContain("Glooko bolus event stream");
    expect(screen.getByText("Carbs logged")).toBeTruthy();
    expect(screen.queryByText(/Bolus\/basal split not available/)).toBeNull();
  });
});

describe("visit report summary page", () => {
  const summarized = {
    ...report,
    contradictions: { unresolved: [] },
    glucose: {
      ...report.glucose,
      tbr70: 10.3,
      tbr54: 1.6,
      low_episodes: { episodes: 233, level2_episodes: 51, nocturnal_episodes: 7, per_week: 17.9 },
    },
    history: {
      narrative: null,
      events: [
        { date: "2026-04-20", type: "note", title: "Reported symptoms: bedtime", details: "Glucose rises after 9 pm for 2.5 hours. Synthetic second sentence." },
        { date: "2026-04-20", type: "note", title: "Reported symptoms: appetite", details: "No hunger cues. Synthetic appetite detail." },
        { date: "2026-01-15", type: "prescription", title: "Synthetic single injection", details: "One dose." },
      ],
    },
    labs: {
      available: true,
      flagged: [
        { test_name: "Synthetic antibody", value: 212, unit: "IU/mL", reference_high: 9, flag: "high", date: "2026-04-24", verification: {} },
        { test_name: "Synthetic peptide", value: 0.16, unit: "ng/mL", reference_low: 0.8, reference_high: 3.85, flag: "low", date: "2026-03-01", verification: {} },
      ],
      key_results: [],
      categories: {
        Thyroid: [{ test_name: "Synthetic antibody", value: 212, unit: "IU/mL", flag: "high", date: "2026-04-24", verification: {} }],
        Chemistry: [
          { test_name: "Synthetic peptide", value: 0.16, unit: "ng/mL", flag: "low", date: "2026-03-01", verification: {} },
          { test_name: "Synthetic sodium", value: 140, unit: "mmol/L", flag: "normal", date: "2026-03-01", verification: {} },
        ],
      },
      verification: { counts: { approved: 0, edited: 0, unverified: 3 } },
    },
  };

  const month = (key, first, last, tbr70) => ({
    month: key, first_day: first, last_day: last, days: 30, readings: 8000, coverage_pct: 99,
    avg: 110, gmi: 6, cv: 26.4, tir: 90.2, tbr70, tbr54: 1, tar180: 1.9, tar250: 0, low_episodes: 70,
  });
  const historied = {
    ...summarized,
    insulin: { available: true, quality: {}, nutrition_quality: {}, boluses_per_day: 0.3, bolus_stream_incomplete: true },
    trends: {
      glucose_by_month: [
        month("2026-02", "2026-02-26", "2026-02-28", 0),
        month("2026-03", "2026-03-01", "2026-03-31", 18.3),
        month("2026-04", "2026-04-01", "2026-04-22", 5),
      ],
      insulin_by_month: [
        { month: "2026-03", days: 31, tdd: 37.2, basal: 21.6, bolus: 15.6, weight_kg: 92.1, weight_date: "2026-03-15", weight_carried: false, units_per_kg: 0.4 },
        { month: "2026-04", days: 21, tdd: 38.7, basal: 38.6, bolus: 0.1, weight_kg: 78.9, weight_date: "2026-03-30", weight_carried: true, units_per_kg: 0.49 },
      ],
      a1c_vs_gmi: [
        { date: "2026-02-11", a1c: 6.6, gmi: 7.3, difference: -0.7, cgm_days: 44 },
        { date: "2026-04-01", a1c: 4.9, gmi: 5.9, difference: -1, cgm_days: 90 },
      ],
      lows_by_hour: {
        pct_below_70_by_hour: Array.from({ length: 24 }, (_, hour) => (hour >= 11 && hour < 21 ? 20 : 1)),
        low_readings: 2600,
        span: { start_hour: 11, end_hour: 21, share_pct: 83 },
        overnight: { nights: 90, nights_with_low: 10, hours: "00:00–06:00" },
      },
      pump_use: {
        modes: {
          days_with_mode_records: 91, automated_pct: 51, manual_pct: 49,
          manual_share_by_hour: Array.from({ length: 24 }, (_, hour) => (hour >= 8 && hour < 19 ? 100 : 0)),
          manual_span: { start_hour: 8, end_hour: 19, hours: 11 },
        },
        temp_basals: { count: 431, per_day: 4.7, median_increase_pct: 95, median_minutes: 38 },
      },
    },
    activity_position: {
      ...report.activity_position,
      walking_response: {
        walks_measured: 1071,
        median_walk_minutes: 21,
        bands: {
          under_120: { walking: { n: 796, delta_120: 2, nadir_delta: -17, went_low_pct: 58 }, sitting: { n: 993, delta_120: 18, went_low_pct: 20 } },
          "120_to_170": {
            walking: { n: 257, delta_120: -43, nadir_delta: -55, went_low_pct: 29, at_stop_delta: -10, after_stop_delta: -45 },
            sitting: { n: 787, delta_120: -10, went_low_pct: 2 },
          },
          over_170: { walking: null, sitting: null },
        },
      },
    },
    imaging: [{
      id: "ct", title: "Synthetic CT abdomen", date: "2026-03-30", summary: "Synthetic finding text.",
      measurements: [{ name: "Spleen length", value: 12.5, unit: "cm", flag: "high" }],
    }],
    labs: {
      ...summarized.labs,
      key_results: [{
        test_name: "Ferritin", value: 16, unit: "ng/mL", reference_low: 16, reference_high: 154, flag: "normal", date: "2026-04-25",
        verification: {},
        history: [
          { parsed_date: "2026-02-11", value: 28, unit: "ng/mL" },
          { parsed_date: "2026-02-11", value: 28, unit: "ng/mL" },
          { parsed_date: "2026-03-01", value: 44.4, unit: "ng/mg" },
          { parsed_date: "2026-03-23", value: 37, unit: "NG/ML" },
          { parsed_date: "2026-04-25", value: 16, unit: "ng/mL" },
        ],
      }],
    },
  };

  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => summarized }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("lists findings computed from the data on the first page", async () => {
    render(<Report />);

    const findings = (await screen.findByText("Findings to review")).closest("div").parentElement;
    expect(findings.textContent).toContain("233 low episodes of 15+ minutes (17.9/week), 7 overnight (00:00–06:00), 51 reaching below 54 mg/dL");
    expect(findings.textContent).toContain("2 results outside the reference range at the latest draw");
    expect(findings.textContent).toContain("Synthetic antibody 212 IU/mL (high, 2026-04-24)");
    expect(findings.textContent).toContain("Reported symptoms. Recorded 2026-04-20.");
    expect(findings.textContent).toContain("Bedtime: Glucose rises after 9 pm for 2.5 hours.");
    expect(findings.textContent).toContain("Appetite: No hunger cues.");
    expect(findings.textContent).not.toContain("Synthetic second sentence.");
  });

  it("prints patient-reported symptoms by title without the record prefix", async () => {
    render(<Report />);

    expect(await screen.findByText("Patient-reported symptoms")).toBeTruthy();
    expect(screen.getByText("Bedtime")).toBeTruthy();
    expect(screen.getByText("Glucose rises after 9 pm for 2.5 hours. Synthetic second sentence.")).toBeTruthy();
    expect(screen.getByText("Synthetic single injection")).toBeTruthy();
  });

  it("states lab verification once instead of on every row", async () => {
    render(<Report />);

    expect(await screen.findByText(/All lab values on file are machine-extracted/)).toBeTruthy();
    expect(screen.queryByText("unverified")).toBeNull();
  });

  it("prints out-of-range labs and leaves the full list off paper until included", async () => {
    render(<Report />);

    const table = (await screen.findByText(/Outside the reference range at the latest draw/)).closest(".report-table");
    const rows = Array.from(table.querySelectorAll("tbody tr")).map((row) => row.textContent);
    expect(rows[0]).toContain("Synthetic antibody");
    expect(rows[1]).toContain("Synthetic peptide");
    expect(table.textContent).not.toContain("Synthetic sodium");

    const allLabs = screen.getByRole("button", { name: /All lab results \(3 tests\)/ }).closest("section");
    expect(allLabs.className).toContain("print:hidden");
    expect(allLabs.textContent).toContain("Synthetic sodium");
    fireEvent.click(screen.getByLabelText("Print all lab results"));
    expect(allLabs.className).not.toContain("print:hidden");

    const analyses = screen.getByRole("button", { name: /Additional analyses/ }).closest("section");
    expect(analyses.className).toContain("print:hidden");
  });

  it("adds the monthly low trend and the A1c–GMI gap to the findings", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(/** @type {Response} */ ({ ok: true, json: async () => historied }));
    render(<Report />);

    const findings = (await screen.findByText("Findings to review")).closest("div").parentElement;
    expect(findings.textContent).toContain("Below 70 by month: Feb 0%, Mar 18.3%, Apr 5%.");
    expect(findings.textContent).toContain(
      "A1c is lower than the CGM-estimated GMI at every draw: 6.6% vs 7.3% (2026-02-11); 4.9% vs 5.9% (2026-04-01).",
    );
  });

  it("tables glucose and insulin month by month", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(/** @type {Response} */ ({ ok: true, json: async () => historied }));
    render(<Report />);

    const glucose = (await screen.findByText("Glucose by month")).closest(".report-table");
    const glucoseRows = Array.from(glucose.querySelectorAll("tbody tr")).map((row) => row.textContent);
    expect(glucoseRows[0]).toContain("Feb 26–28, 2026");
    expect(glucoseRows[1]).toContain("Mar 2026");
    expect(glucoseRows[1]).toContain("18.3%");
    expect(glucoseRows[2]).toContain("Apr 1–22, 2026");
    expect(glucoseRows[2]).toContain("6.0%");

    const insulin = screen.getByText("Insulin by month").closest(".report-table");
    const insulinRows = Array.from(insulin.querySelectorAll("tbody tr")).map((row) => row.textContent);
    expect(insulinRows[0]).toContain("37.2");
    expect(insulinRows[0]).toContain("0.40");
    expect(insulinRows[1]).toContain("173.9 lb†");
    expect(insulin.textContent).toContain("No weight logged that month");
  });

  it("shows when lows happen, what walking does, and how the pump is run", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(/** @type {Response} */ ({ ok: true, json: async () => historied }));
    render(<Report />);

    const lows = (await screen.findByText("When lows happen")).closest(".report-card");
    expect(lows.textContent).toContain("83% of readings below 70 fell between 11:00 and 21:00.");
    expect(lows.textContent).toContain("Nights with any reading below 70 between 00:00 and 06:00: 10 of 90.");
    const walking = Array.from(lows.querySelectorAll("tbody tr")).map((row) => row.textContent);
    expect(walking).toHaveLength(2);
    expect(walking[1]).toContain("121–170");
    expect(walking[1]).toContain("−43");
    expect(walking[1]).toContain("29%");
    expect(lows.textContent).toContain("−10 mg/dL by the end of the walk, then a further −45 after it ended.");

    const pump = screen.getByText("How the pump is used").closest(".report-card");
    expect(pump.textContent).toContain("Manual mode in most of each hour from 08:00 to 19:00");
    expect(pump.textContent).toContain("4.7 per day (431 in the window), median +95%");
  });

  it("lists A1c beside GMI, imaging, and earlier results of a key lab in its own unit", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(/** @type {Response} */ ({ ok: true, json: async () => historied }));
    render(<Report />);

    const a1c = (await screen.findByText("A1c and CGM-estimated GMI")).closest(".report-table");
    expect(a1c.textContent).toContain("−1.0");

    const imaging = screen.getByText("Synthetic CT abdomen").closest(".report-card");
    expect(imaging.textContent).toContain("Synthetic finding text.");
    expect(imaging.textContent).toContain("Spleen length: 12.5 cm");

    const ferritin = screen.getByText("Ferritin").closest("tr");
    expect(ferritin.textContent).toContain("28 (2026-02-11) · 37 (2026-03-23)");
    expect(ferritin.textContent).not.toContain("44.4");
  });
});

describe("visit report specialty views", () => {
  const specialties = [
    { key: "endocrinology", label: "Endocrinology", domains: ["glucose", "insulin", "analytics", "labs", "clinical"], keywords: ["thyroid", "glucose"], omit_unconfirmed_hypotheses: false },
    { key: "gastroenterology", label: "Gastroenterology", domains: ["clinical", "labs", "records", "analytics"], keywords: ["liver", "celiac"], omit_unconfirmed_hypotheses: false },
    { key: "primary_care", label: "Primary care", domains: ["clinical", "labs", "records", "glucose", "insulin", "analytics", "wearables", "cycle"], keywords: [], omit_unconfirmed_hypotheses: true },
  ];
  const lab = (test_name, extra = {}) => ({
    test_name, value: 1, unit: "U/L", flag: "high", date: "2026-04-01", verification: {}, ...extra,
  });
  const viewed = {
    ...report,
    contradictions: { unresolved: [] },
    specialties,
    insurance: { available: true, carrier: "Synthetic Carrier", member_name: "Synthetic Member" },
    insulin: { available: true, quality: {}, nutrition_quality: {}, pump_reported_avg_tdd: 38, pump_reported_days: 30, bolus_stream_incomplete: true },
    labs: {
      available: true,
      flagged: [lab("Synthetic ALT"), lab("Synthetic TPO")],
      key_results: [lab("Synthetic A1c", { flag: "normal", source: { record_id: "rec-1", page: 2 } })],
      categories: {
        "Hepatic Function (liver)": [lab("Synthetic ALT")],
        Thyroid: [lab("Synthetic TPO")],
      },
      verification: {},
    },
    imaging: [{ id: "mri", title: "Synthetic brain MRI", date: "2026-04-10", summary: "Unremarkable.", measurements: [] }],
    documents: [
      { id: "doc-liver", title: "Synthetic liver panel", type: "Lab report", date: "2026-04-01", summary: "Synthetic liver summary.", lab_count: 7 },
      { id: "doc-thyroid", title: "Synthetic thyroid panel", type: "Lab report", date: "2026-03-01", summary: null, lab_count: 3 },
    ],
  };

  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => viewed }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    window.history.replaceState({}, "", "/");
  });

  it("is the full report until a specialty is chosen", async () => {
    render(<Report />);

    const select = await screen.findByLabelText("Report view");
    expect(select).toHaveProperty("value", "");
    expect(Array.from(select.querySelectorAll("option")).map((o) => o.textContent)).toEqual([
      "Full report", "Endocrinology", "Gastroenterology", "Primary care",
    ]);
    expect(screen.getByText("Insulin & carbs")).toBeTruthy();
    expect(screen.getByText("Synthetic Carrier")).toBeTruthy();
    expect(screen.getByText("Synthetic brain MRI")).toBeTruthy();
  });

  it("shows a specialty's domains and keyword-matched labs and documents, without insurance", async () => {
    render(<Report />);

    fireEvent.change(await screen.findByLabelText("Report view"), { target: { value: "gastroenterology" } });

    expect(screen.getByText(/Gastroenterology view/)).toBeTruthy();
    expect(screen.queryByText("Insulin & carbs")).toBeNull();
    expect(screen.queryByText("Time below 70 mg/dL")).toBeNull();
    const outOfRange = screen.getByText(/Outside the reference range/).closest(".report-table");
    expect(outOfRange.textContent).toContain("Synthetic ALT");
    expect(outOfRange.textContent).not.toContain("Synthetic TPO");
    expect(screen.getByText("Synthetic liver panel")).toBeTruthy();
    expect(screen.queryByText("Synthetic thyroid panel")).toBeNull();
    // Imaging is kept in every view that lists documents.
    expect(screen.getByText("Synthetic brain MRI")).toBeTruthy();
    expect(screen.queryByText("Synthetic Carrier")).toBeNull();
  });

  it("opens the view named in the address", async () => {
    window.history.replaceState({}, "", "/report?for=endocrinology");
    render(<Report />);

    expect(await screen.findByText(/Endocrinology view/)).toBeTruthy();
    expect(screen.getByText("Insulin & carbs")).toBeTruthy();
    // Endocrinology has no documents domain of its own; its labs domain keeps them, keyword-matched.
    expect(screen.getByText("Synthetic thyroid panel")).toBeTruthy();
    expect(screen.queryByText("Synthetic liver panel")).toBeNull();
  });

  it("links results and documents to the uploaded document they came from", async () => {
    render(<Report />);

    const keyRow = (await screen.findByText("Synthetic A1c")).closest("tr");
    const keyLink = Array.from(keyRow.querySelectorAll("a")).find((a) => a.textContent === "Source");
    expect(keyLink.getAttribute("href")).toBe("/api/records/file/rec-1?inline=true#page=2");
    expect(keyLink.className).toContain("print:hidden");

    const docRow = screen.getByText("Synthetic liver panel").closest("tr");
    expect(docRow.querySelector("a").getAttribute("href")).toBe("/api/records/file/doc-liver?inline=true");
    expect(docRow.textContent).toContain("7 results");
    expect(docRow.querySelector("details").className).toContain("print:hidden");
    expect(screen.getByText("Synthetic brain MRI").querySelector("a").getAttribute("href")).toBe("/api/records/file/mri?inline=true");
  });
});

describe("visit report points to discuss", () => {
  const specialties = [
    { key: "primary_care", label: "Primary care", domains: ["clinical", "labs", "records", "glucose", "insulin"], keywords: [], omit_unconfirmed_hypotheses: true },
  ];
  const discussed = {
    ...report,
    contradictions: { unresolved: [] },
    specialties,
    narrative: {
      headline: "Synthetic headline.",
      discussion_points: ["Synthetic suggestion about lows.", "Synthetic suggestion about oxygen."],
    },
    agenda: {
      primary_care: { points: ["Synthetic nutrition question."], updated_at: "2026-10-22T12:00:00Z" },
    },
  };
  const asOwner = (ui) => render(<AuthContext.Provider value={{ isAdmin: true }}>{ui}</AuthContext.Provider>);

  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    vi.stubGlobal("fetch", vi.fn(async (url, options = {}) => {
      if (String(url) === "/api/report/agenda") {
        const body = JSON.parse(String(options.body));
        return { ok: true, json: async () => ({ agenda: { ...discussed.agenda, [body.view]: { points: body.points, updated_at: "2026-10-22T13:00:00Z" } } }) };
      }
      return { ok: true, json: async () => discussed };
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    window.history.replaceState({}, "", "/");
  });

  const pointsCard = () => screen.getByRole("heading", { name: "Points to discuss" }).closest(".report-card");

  it("shows the suggestions in the full report, read-only without the owner login", async () => {
    render(<Report />);

    expect(await screen.findByText("Synthetic suggestion about lows.")).toBeTruthy();
    expect(pointsCard().textContent).toContain("Suggested from the data");
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
  });

  it("prints the points saved for a view in place of the suggestions", async () => {
    window.history.replaceState({}, "", "/report?for=primary_care");
    render(<Report />);

    expect(await screen.findByText("Synthetic nutrition question.")).toBeTruthy();
    expect(screen.queryByText("Synthetic suggestion about lows.")).toBeNull();
    expect(pointsCard().textContent).toContain("Edited");
  });

  it("lets the owner edit and save the points for the view on screen", async () => {
    asOwner(<Report />);

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const editor = screen.getByLabelText("Points to discuss");
    expect(editor).toHaveProperty("value", "Synthetic suggestion about lows.\nSynthetic suggestion about oxygen.");
    fireEvent.change(editor, { target: { value: "Nutrition: is she getting the nutrients she needs?\n\n" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Nutrition: is she getting the nutrients she needs?")).toBeTruthy();
    const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url) === "/api/report/agenda");
    expect(call[1].method).toBe("PUT");
    expect(JSON.parse(String(call[1].body))).toEqual({
      view: "full",
      points: ["Nutrition: is she getting the nutrients she needs?", "", ""],
    });
    await waitFor(() => expect(screen.queryByText("Synthetic suggestion about lows.")).toBeNull());
  });
});
