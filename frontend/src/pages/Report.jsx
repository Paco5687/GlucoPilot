import { useState, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import EvidenceContextBlock from "@/components/evidence/EvidenceContextBlock";
import { Loader2, Printer, FileText, RefreshCw, TrendingUp, TrendingDown, Minus, AlertTriangle, ShieldCheck, Beaker, ChevronDown, ChevronRight, ClipboardList } from "lucide-react";
import {
  ResponsiveContainer, AreaChart, Area, Bar, BarChart, Line, LineChart, XAxis, YAxis, Tooltip, ReferenceLine, CartesianGrid,
} from "recharts";

// Clinical AGP color convention (not brand palette) — this is a medical report.
const TIR_COLORS = {
  veryLow: "#b91c1c",
  low: "#ef4444",
  inRange: "#16a34a",
  high: "#f59e0b",
  veryHigh: "#d97706",
};

// The printout leads with a one-page summary; the clinical sections follow it
// and flow across pages. Long tables break between rows, headings stay with
// what follows them, and optional material stays off paper unless included.
const PRINT_CSS = `
@media print {
  html { font-size: 12.5px; }
  html, body, main { background: #fff !important; }
  header, .print\\:hidden { display: none !important; }
  main { padding: 0 !important; max-width: 100% !important; }
  .report-card { break-inside: avoid; box-shadow: none !important; border-color: #ddd !important; }
  .report-table { box-shadow: none !important; border-color: #ddd !important; }
  .report-table tr, .report-section { break-inside: avoid; }
  .report-page-break { break-before: page; }
  h2, h3 { break-after: avoid; }
  body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  @page { margin: 1.2cm; }
}
`;

// International consensus CGM targets (Battelino et al., Diabetes Care 2019).
const TIR_TARGET = 70;
const TBR70_TARGET = 4;
const TBR54_TARGET = 1;
const TAR180_TARGET = 25;
const TAR250_TARGET = 5;
const CV_TARGET = 36;
const LB_PER_KG = 2.20462;
const SIGNIFICANT_WEIGHT_LOSS_PCT = 5;
const FINDING_LABS_LISTED = 5;
const FINDING_MONTHS_LISTED = 6;
// A1c and GMI further apart than this, in one direction at every draw, is a finding.
const A1C_GMI_GAP = 0.5;
const REPORTED_SYMPTOMS = /^\s*reported symptoms?:?\s*/i;

function plural(count, word, suffix = "s") {
  return `${count} ${word}${count === 1 ? "" : suffix}`;
}

function Metric({ label, value, sub = null, tone = "" }) {
  return (
    <div className="report-card bg-card rounded-lg border border-border p-3">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className={`text-2xl font-bold tabular-nums ${tone || ""}`}>{value}</div>
      {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

function SectionTitle({ children, sub = null }) {
  return (
    <div>
      <h2 className="font-semibold text-base">{children}</h2>
      {sub && <p className="text-[11px] text-muted-foreground">{sub}</p>}
    </div>
  );
}

// Optional material: collapsed on screen and left out of the printout unless
// the print option for it is ticked. The content stays in the page so the
// print option can include it without expanding it on screen.
function OptionalSection({ title, summary = null, includeInPrint, children }) {
  const [open, setOpen] = useState(false);
  return (
    <section className={`report-section rounded-lg border border-dashed border-border bg-muted/20 ${includeInPrint ? "" : "print:hidden"}`}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="print:hidden w-full flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-left"
      >
        <span className="inline-flex items-center gap-2 text-sm font-medium">
          {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          {title}
        </span>
        {summary && <span className="text-[11px] text-muted-foreground">{summary}</span>}
      </button>
      <h2 className="hidden print:block font-semibold text-base px-3 pt-2">{title}</h2>
      <div className={`${open ? "" : "hidden"} print:block px-3 pb-3 space-y-4`}>{children}</div>
    </section>
  );
}

function settingValue(value) {
  return Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
}

function SettingsTable({ title, unit, segments }) {
  return (
    <div className="report-card bg-card rounded-lg border border-border p-2.5">
      <p className="text-xs font-medium mb-1">{title} <span className="font-normal text-muted-foreground">({unit})</span></p>
      <table className="w-full text-[11px] tabular-nums">
        <tbody>
          {segments.map((s) => (
            <tr key={`${s.start}-${s.end}`} className="border-t border-border/50">
              <td className="py-0.5">{s.start}–{s.end}</td>
              <td className="py-0.5 text-right font-medium">{settingValue(s.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PumpSettingsSection({ ps }) {
  const programs = [...(ps.basal_programs || [])].sort((a, b) => Number(b.active) - Number(a.active));
  return (
    <div className="space-y-2">
      <SectionTitle
        sub={`As of ${ps.as_of}${ps.device_model ? ` · ${ps.device_model}` : ""} · the most recent settings snapshot Glooko recorded, ${plural(ps.age_days, "day")} before this report. Changes made on the pump after that date are not reflected.`}
      >
        Programmed pump settings
      </SectionTitle>
      {ps.scalars?.length > 0 && (
        <p className="text-xs">
          {ps.scalars.map((s) => `${s.label}: ${settingValue(s.value)} ${s.unit}`).join(" · ")}
        </p>
      )}
      <div className="grid grid-cols-2 md:grid-cols-4 print:grid-cols-4 gap-2">
        {programs.map((p) => (
          <SettingsTable
            key={p.name}
            title={`Basal: ${p.name}${p.active ? " (active)" : ""}`}
            unit={`U/h · ${settingValue(p.units_per_day)} U/day`}
            segments={p.segments}
          />
        ))}
        {(ps.schedules || []).map((s) => (
          <SettingsTable key={s.key} title={s.label} unit={s.unit} segments={s.segments} />
        ))}
      </div>
      {ps.changes?.length > 0 && (
        <p className="text-[11px] text-muted-foreground">
          Recorded changes: {ps.changes.map((c) => `${c.snapshot_at.slice(0, 10)} (${c.changed.join(", ")})`).join("; ")}.
        </p>
      )}
    </div>
  );
}

function TIRBar({ g }) {
  // Non-overlapping band widths from cumulative thresholds.
  const segs = [
    { label: "Very low (<54)", pct: g.tbr54, color: TIR_COLORS.veryLow },
    { label: "Low (54–69)", pct: Math.max(0, g.tbr70 - g.tbr54), color: TIR_COLORS.low },
    { label: "In range (70–180)", pct: g.tir, color: TIR_COLORS.inRange },
    { label: "High (181–250)", pct: Math.max(0, g.tar180 - g.tar250), color: TIR_COLORS.high },
    { label: "Very high (>250)", pct: g.tar250, color: TIR_COLORS.veryHigh },
  ];
  return (
    <div>
      <div className="flex h-7 rounded-md overflow-hidden border border-border">
        {segs.map((s, i) => (
          s.pct > 0 ? (
            <div key={i} style={{ width: `${s.pct}%`, background: s.color }} title={`${s.label}: ${s.pct.toFixed(1)}%`} />
          ) : null
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-[11px]">
        {segs.map((s, i) => (
          <span key={i} className="flex items-center gap-1">
            <i className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: s.color }} />
            {s.label}: <b>{s.pct.toFixed(1)}%</b>
          </span>
        ))}
      </div>
    </div>
  );
}

function FingerstickReconciliation({ g }) {
  const reconciliation = g.fingerstick_reconciliation;
  if (!reconciliation?.paired) return null;
  const lows = reconciliation.low_reconciliation;
  const bias = reconciliation.persistent_bias;
  const minimum = bias?.minimum_sample_count || 5;
  // Below the sample needed to judge bias, one line says what was recorded.
  if ((bias?.sample_count || reconciliation.paired) < minimum) {
    return (
      <p className="text-xs text-muted-foreground">
        Meter checks: {plural(reconciliation.paired, "fingerstick")} paired with CGM in this period
        (mean difference {reconciliation.mean_abs_delta} mg/dL); {minimum} are needed to assess CGM bias.
      </p>
    );
  }
  const biasLabel = {
    cgm_high: "persistent CGM-high direction observed",
    cgm_low: "persistent CGM-low direction observed",
    not_detected: "no persistent directional bias detected",
    insufficient_sample: "insufficient sample for persistent bias",
  }[bias?.classification] || "not assessed";
  return (
    <div className="report-card bg-card rounded-lg border border-border p-3 space-y-2">
      <div>
        <p className="text-xs font-medium">CGM and meter reconciliation</p>
        <p className="text-[11px] text-muted-foreground">{reconciliation.semantics}</p>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
        <div><span className="text-muted-foreground">Paired checks</span><br /><b>{reconciliation.paired}</b></div>
        <div><span className="text-muted-foreground">Mean absolute gap</span><br /><b>{reconciliation.mean_abs_delta} mg/dL</b></div>
        <div><span className="text-muted-foreground">Confirmed lows</span><br /><b>{lows?.confirmed_low || 0}</b></div>
        <div><span className="text-muted-foreground">CGM-only lows</span><br /><b>{lows?.cgm_only_low || 0}</b></div>
      </div>
      <p className="text-[11px]">
        Directional bias: <b>{biasLabel}</b> ({bias?.sample_count || 0} paired checks; minimum {minimum}).
      </p>
      <p className="text-[11px] text-muted-foreground">{lows?.caveat}</p>
    </div>
  );
}

function AGPChart({ agp }) {
  const data = agp
    .filter((h) => h.p50 != null)
    .map((h) => ({
      hour: h.hour,
      band: [h.p5, h.p95],
      iqr: [h.p25, h.p75],
      p50: h.p50,
    }));
  return (
    <div className="h-52">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" vertical={false} />
          <XAxis dataKey="hour" tickFormatter={(h) => `${h}:00`} tick={{ fontSize: 10 }} interval={2} />
          <YAxis domain={[40, 300]} ticks={[54, 70, 180, 250]} tick={{ fontSize: 10 }} width={32} />
          <Tooltip
            formatter={(v, n) => [Array.isArray(v) ? `${v[0]}–${v[1]} mg/dL` : `${v} mg/dL`, n === "p50" ? "median" : n === "band" ? "5–95%" : "25–75%"]}
            labelFormatter={(h) => `${h}:00`}
            contentStyle={{ fontSize: 12, borderRadius: 8 }}
          />
          <ReferenceLine y={70} stroke="#16a34a" strokeDasharray="4 4" strokeOpacity={0.6} />
          <ReferenceLine y={180} stroke="#16a34a" strokeDasharray="4 4" strokeOpacity={0.6} />
          <Area dataKey="band" stroke="none" fill="#2563eb" fillOpacity={0.1} isAnimationActive={false} />
          <Area dataKey="iqr" stroke="none" fill="#2563eb" fillOpacity={0.22} isAnimationActive={false} />
          <Line dataKey="p50" stroke="#1d4ed8" strokeWidth={2} dot={false} type="monotone" isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

const TREND_ICON = { up: TrendingUp, down: TrendingDown, flat: Minus };

function ContradictionSide({ side }) {
  const value = side?.value != null ? `${side.value}${side.unit ? ` ${side.unit}` : ""}` : null;
  const range = side?.reference_low != null || side?.reference_high != null
    ? `${side.reference_low ?? "—"}–${side.reference_high ?? "—"}${side.unit ? ` ${side.unit}` : ""}`
    : null;
  const phases = side?.expected_cycle_phases || side?.cycle_phases;
  return (
    <div className="rounded-md border border-border bg-background p-2">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{side?.label || "Evidence"}</div>
      <div className="text-sm font-medium">{side?.name || value || phases?.join(", ") || side?.source || "Source evidence"}</div>
      {side?.name && value && <div className="text-xs">{value}</div>}
      {range && <div className="text-xs">Reference range: {range}</div>}
      {side?.name && phases?.length > 0 && <div className="text-xs">{phases.join(", ")}</div>}
      {side?.observed_at && <div className="text-[11px] text-muted-foreground">{side.observed_at}</div>}
    </div>
  );
}

const HYPOTHESIS_STATE = {
  not_evaluated: { label: "Not evaluated", tone: "border-slate-200 bg-slate-50 text-slate-700" },
  preliminary: { label: "Preliminary", tone: "border-amber-200 bg-amber-50 text-amber-800" },
  mixed: { label: "Mixed evidence", tone: "border-blue-200 bg-blue-50 text-blue-800" },
  leans_supportive: { label: "Evidence leans supportive", tone: "border-emerald-200 bg-emerald-50 text-emerald-800" },
  leans_against: { label: "Evidence leans against", tone: "border-slate-300 bg-slate-100 text-slate-800" },
  clinician_confirmed: { label: "Clinician confirmed", tone: "border-emerald-300 bg-emerald-50 text-emerald-900" },
  clinician_ruled_against: { label: "Clinician ruled against", tone: "border-slate-300 bg-slate-100 text-slate-800" },
};

function hypothesisState(hypothesis) {
  if (hypothesis.evidence_state && HYPOTHESIS_STATE[hypothesis.evidence_state]) {
    return hypothesis.evidence_state;
  }
  if (hypothesis.status === "confirmed") return "clinician_confirmed";
  if (hypothesis.status === "ruled_against") return "clinician_ruled_against";
  const supporting = hypothesis.evidence_by_role?.supporting || [];
  const opposing = hypothesis.evidence_by_role?.opposing || [];
  if (!supporting.length && !opposing.length) return "not_evaluated";
  if (supporting.length + opposing.length < 2) return "preliminary";
  const supportingWeight = supporting.reduce((sum, item) => sum + Number(item.weight || 1), 0);
  const opposingWeight = opposing.reduce((sum, item) => sum + Number(item.weight || 1), 0);
  const balance = supportingWeight / (supportingWeight + opposingWeight);
  return balance >= 0.65 ? "leans_supportive" : balance <= 0.35 ? "leans_against" : "mixed";
}

function EvidenceDetails({ hypothesis }) {
  return (
    <details className="rounded border border-border bg-background/60 print:hidden">
      <summary className="cursor-pointer px-3 py-2 text-xs font-medium">
        Review recorded evidence
      </summary>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 px-3 pb-3 text-xs">
        {[
          ["Supporting", "supporting", "border-emerald-200 bg-emerald-50"],
          ["Opposing", "opposing", "border-rose-200 bg-rose-50"],
          ["Missing / needed", "missing", "border-amber-200 bg-amber-50"],
        ].map(([label, role, tone]) => {
          const evidence = hypothesis.evidence_by_role?.[role] || [];
          return (
            <div key={role} className={`rounded border p-2 ${tone}`}>
              <p className="font-medium">{label}</p>
              {evidence.length ? (
                <ul className="mt-1 list-disc pl-4 space-y-0.5">
                  {evidence.map((item, index) => <li key={index}>{item.summary}</li>)}
                </ul>
              ) : (
                <p className="mt-1 text-muted-foreground">None recorded.</p>
              )}
            </div>
          );
        })}
      </div>
    </details>
  );
}

function PatientLine({ patient }) {
  if (!patient?.name && !patient?.date_of_birth) return null;
  const details = [
    patient.date_of_birth ? `DOB ${patient.date_of_birth}` : null,
    patient.age != null ? `${patient.age} y` : null,
    patient.sex ? patient.sex.charAt(0).toUpperCase() + patient.sex.slice(1) : null,
  ].filter(Boolean);
  return (
    <p className="text-base mt-1">
      {patient.name && <span className="font-semibold">{patient.name}</span>}
      {patient.name && details.length > 0 && " · "}
      {details.join(" · ")}
    </p>
  );
}

// Coverage of each data source in one line under the header, in place of a
// data-quality box per section. Low-reliability sources are marked.
function DataSourcesLine({ report }) {
  const sources = [
    ["CGM", report.glucose?.quality],
    ["Pump daily totals", report.insulin?.available ? report.insulin.quality : null],
    ["Oura", report.wellness?.oura ? report.wellness.quality?.oura : null],
    ["Fitbit", report.wellness?.fitbit ? report.wellness.quality?.fitbit : null],
  ].filter(([, quality]) => quality?.coverage_pct != null);
  if (!sources.length) return null;
  return (
    <p className="text-[11px] text-muted-foreground mt-1">
      Data coverage:{" "}
      {sources.map(([label, quality], index) => (
        <span key={label} className={quality.reliability === "low" ? "text-amber-700" : ""}>
          {index > 0 && " · "}
          {label} {quality.coverage_pct}%{quality.data_through ? ` through ${quality.data_through}` : ""}
        </span>
      ))}
    </p>
  );
}

function formatWeight(kg, units) {
  if (kg == null) return "—";
  return units === "metric" ? `${kg.toFixed(1)} kg` : `${(kg * LB_PER_KG).toFixed(1)} lb`;
}

function formatWeightChange(change, units) {
  if (!change) return "—";
  const sign = change.change_kg > 0 ? "+" : change.change_kg < 0 ? "−" : "";
  const magnitude = Math.abs(change.change_kg);
  const amount = units === "metric" ? `${magnitude.toFixed(1)} kg` : `${(magnitude * LB_PER_KG).toFixed(1)} lb`;
  return `${sign}${amount} (${sign}${Math.abs(change.change_pct).toFixed(1)}%)`;
}

function weightTone(change) {
  return change && change.change_pct <= -SIGNIFICANT_WEIGHT_LOSS_PCT ? "text-red-600" : "";
}

function significantWeightLoss(weight) {
  return [weight?.peak_change, weight?.window_change].some(
    (change) => change && change.change_pct <= -SIGNIFICANT_WEIGHT_LOSS_PCT,
  );
}

function WeightBlock({ weight }) {
  if (!weight?.available) return null;
  const units = weight.units;
  const series = (weight.series || []).map((point) => ({
    date: point.date,
    value: units === "metric" ? point.weight_kg : Math.round(point.weight_kg * LB_PER_KG * 10) / 10,
  }));
  // The 12-month high is the headline reference; the first entry can be a
  // years-old baseline and only appears as secondary context.
  return (
    <div className="report-section space-y-3">
      <SectionTitle>Weight</SectionTitle>
      <div className="grid grid-cols-1 sm:grid-cols-3 print:grid-cols-3 gap-3">
        <Metric label="Latest weight" value={formatWeight(weight.latest?.weight_kg, units)} sub={weight.latest?.date} />
        <Metric
          label="Change from 12-month high"
          value={formatWeightChange(weight.peak_change, units)}
          sub={
            weight.peak_change
              ? `from ${formatWeight(weight.peak_change.from_kg, units)} on ${weight.peak_change.from_date} · ${weight.peak_change.days} days`
              : "latest is the 12-month high"
          }
          tone={weightTone(weight.peak_change)}
        />
        <Metric
          label="Change over report window"
          value={formatWeightChange(weight.window_change, units)}
          sub={weight.window_change ? `since ${weight.window_change.from_date}` : "no earlier entry"}
          tone={weightTone(weight.window_change)}
        />
      </div>
      {weight.since_first_change && (
        <p className="text-xs text-muted-foreground">
          First logged weight: {formatWeight(weight.first?.weight_kg, units)} on {weight.since_first_change.from_date}{" "}
          ({formatWeightChange(weight.since_first_change, units)} to latest).
        </p>
      )}
      {significantWeightLoss(weight) && (
        <p className="text-xs font-medium text-red-700">
          Weight loss of {SIGNIFICANT_WEIGHT_LOSS_PCT}% or more of body weight is recorded.
        </p>
      )}
      {series.length > 1 && (
        <div className="report-card bg-card rounded-lg border border-border p-3">
          <p className="text-xs font-medium mb-2">
            Logged weight · {weight.entries} entr{weight.entries === 1 ? "y" : "ies"} ({units === "metric" ? "kg" : "lb"})
          </p>
          <div className="h-32">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={series} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" vertical={false} />
                <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={24} />
                <YAxis domain={["auto", "auto"]} tick={{ fontSize: 10 }} width={36} />
                <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8 }} />
                <Line dataKey="value" stroke="#1d4ed8" strokeWidth={2} dot={{ r: 2 }} type="monotone" isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </div>
  );
}

function labResult(entry) {
  return `${entry.value}${entry.unit ? ` ${entry.unit}` : ""}`;
}

function labReference(entry) {
  if (entry.reference_low != null && entry.reference_high != null) return `${entry.reference_low}–${entry.reference_high}`;
  if (entry.reference_high != null) return `≤ ${entry.reference_high}`;
  if (entry.reference_low != null) return `≥ ${entry.reference_low}`;
  return "—";
}

function labDate(entry) {
  return entry.date || entry.event_time?.source_text || entry.collected_date || "date unknown";
}

function labFlagTone(flag) {
  if (!flag || flag === "normal") return "text-green-700";
  return "text-red-600";
}

// Only when verified and unverified results are mixed does a per-row status
// add anything; otherwise the section note covers every row.
function VerificationBadge({ entry, perRow }) {
  if (!perRow || !entry.verification?.qualified) return null;
  return <span className="ml-1.5 text-[9px] uppercase text-amber-700">{entry.verification.verification_status}</span>;
}

const EARLIER_RESULTS_SHOWN = 3;

// Earlier dated results of the same test in the latest result's unit,
// oldest first, without the latest and without repeats of one draw. A result
// in another unit is usually another specimen (urine, saliva) under the same
// name, so it stays out of the series.
function earlierResults(entry) {
  const unit = String(entry.unit || "").toLowerCase();
  const seen = new Set([`${entry.date}|${entry.value}`]);
  const earlier = [];
  for (const point of entry.history || []) {
    const key = `${point.parsed_date}|${point.value}`;
    if (!point.parsed_date || seen.has(key) || String(point.unit || "").toLowerCase() !== unit) continue;
    seen.add(key);
    earlier.push(point);
  }
  return earlier.slice(-EARLIER_RESULTS_SHOWN).map((point) => `${point.value} (${point.parsed_date})`);
}

function KeyLabResults({ results, perRow }) {
  if (!results?.length) return null;
  return (
    <div className="report-card bg-card rounded-lg border border-border overflow-hidden">
      <div className="bg-muted/50 px-3 py-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Key results · most recent value
      </div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wide text-muted-foreground">
            <th className="px-3 py-0.5 text-left font-medium">Test</th>
            <th className="px-3 py-0.5 text-right font-medium">Latest</th>
            <th className="px-3 py-0.5 text-right font-medium">Reference</th>
            <th className="px-2 py-0.5 text-center font-medium">Flag</th>
            <th className="px-3 py-0.5 text-right font-medium">Collected</th>
            <th className="px-3 py-0.5 text-left font-medium">Earlier results</th>
          </tr>
        </thead>
        <tbody>
          {results.map((t) => (
            <tr key={t.test_name} className="border-t border-border align-top">
              <td className="px-3 py-1 font-medium">
                {t.test_name}
                <VerificationBadge entry={t} perRow={perRow} />
              </td>
              <td className="px-3 py-1 text-right tabular-nums whitespace-nowrap">
                {t.value} <span className="text-muted-foreground">{t.unit}</span>
              </td>
              <td className="px-3 py-1 text-right text-muted-foreground tabular-nums whitespace-nowrap">
                {t.reference_low != null || t.reference_high != null ? `ref ${labReference(t)}` : "no reference range"}
              </td>
              <td className={`px-2 py-1 text-center text-[10px] uppercase font-semibold ${labFlagTone(t.flag)}`}>
                {t.flag || "—"}
              </td>
              <td className="px-3 py-1 text-right text-muted-foreground tabular-nums whitespace-nowrap">
                {t.date || t.collected_date || "date unknown"}
              </td>
              <td className="px-3 py-1 text-[11px] text-muted-foreground tabular-nums">
                {earlierResults(t).join(" · ") || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OutOfRangeLabs({ results, panels, perRow }) {
  if (!results?.length) return null;
  return (
    <div className="report-table bg-card rounded-lg border border-red-200 overflow-hidden">
      <div className="bg-red-50 px-3 py-1.5 text-xs font-semibold uppercase tracking-wide text-red-700 flex items-center gap-1">
        <AlertTriangle className="w-3.5 h-3.5" /> Outside the reference range at the latest draw · newest first
      </div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wide text-muted-foreground">
            <th className="px-3 py-0.5 text-left font-medium">Test</th>
            <th className="px-3 py-0.5 text-right font-medium">Result</th>
            <th className="px-3 py-0.5 text-right font-medium">Reference</th>
            <th className="px-2 py-0.5 text-center font-medium">Flag</th>
            <th className="px-3 py-0.5 text-right font-medium">Collected</th>
            <th className="px-3 py-0.5 text-left font-medium">Panel</th>
          </tr>
        </thead>
        <tbody>
          {results.map((t) => (
            <tr key={t.test_name} className="border-t border-border">
              <td className="px-3 py-0.5 font-medium">
                {t.test_name}
                <VerificationBadge entry={t} perRow={perRow} />
              </td>
              <td className="px-3 py-0.5 text-right tabular-nums font-semibold text-red-700 whitespace-nowrap">{labResult(t)}</td>
              <td className="px-3 py-0.5 text-right tabular-nums text-muted-foreground whitespace-nowrap">{labReference(t)}</td>
              <td className="px-2 py-0.5 text-center text-[10px] uppercase font-semibold text-red-600">{t.flag}</td>
              <td className="px-3 py-0.5 text-right tabular-nums text-muted-foreground whitespace-nowrap">{labDate(t)}</td>
              <td className="px-3 py-0.5 text-[10px] text-muted-foreground max-w-[11rem] truncate" title={panels[t.test_name]}>{panels[t.test_name] || ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AllLabResults({ categories, perRow }) {
  return Object.entries(categories).map(([cat, tests]) => (
    <div key={cat} className="report-table bg-card rounded-lg border border-border overflow-hidden">
      <div className="bg-muted/50 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{cat}</div>
      <table className="w-full text-xs">
        <tbody>
          {tests.map((t) => {
            const TrendIcon = TREND_ICON[t.trend];
            const outOfRange = t.flag && t.flag !== "normal";
            return (
              <tr key={t.test_name} className="border-t border-border align-top">
                <td className="px-3 py-1 font-medium">
                  {t.test_name}
                  <VerificationBadge entry={t} perRow={perRow} />
                  {t.reported_names?.length > 0 && (
                    <div className="text-[10px] font-normal text-muted-foreground">also reported as {t.reported_names.join(", ")}</div>
                  )}
                </td>
                <td className={`px-3 py-1 text-right tabular-nums ${outOfRange ? "text-red-600 font-semibold" : ""}`}>
                  {t.value} <span className="text-muted-foreground font-normal">{t.unit}</span>
                </td>
                <td className="px-3 py-1 text-right text-muted-foreground tabular-nums">
                  {t.reference_low != null && t.reference_high != null ? `${t.reference_low}–${t.reference_high}` : ""}
                </td>
                <td className="px-2 py-1 text-center w-6">
                  {TrendIcon && <TrendIcon className={`w-3.5 h-3.5 inline ${t.trend === "up" ? "text-amber-500" : t.trend === "down" ? "text-sky-500" : "text-muted-foreground"}`} />}
                </td>
                <td className="px-3 py-1 text-right text-muted-foreground tabular-nums whitespace-nowrap">{labDate(t)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  ));
}

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function monthName(month) {
  const [year, number] = month.split("-").map(Number);
  return `${MONTH_NAMES[number - 1]} ${year}`;
}

// "Mar 2026" for a whole month; "Feb 26–28" or "Oct 1–22" when the data
// starts or stops partway through it.
function monthLabel(row) {
  const [year, number] = row.month.split("-").map(Number);
  const lastOfMonth = new Date(year, number, 0).getDate();
  const first = Number(row.first_day.slice(8));
  const last = Number(row.last_day.slice(8));
  if (first === 1 && last === lastOfMonth) return monthName(row.month);
  return `${MONTH_NAMES[number - 1]} ${first}–${last}, ${year}`;
}

function hourLabel(hour) {
  return `${String(hour).padStart(2, "0")}:00`;
}

function TableCard({ title, sub = null, children }) {
  return (
    <div className="report-table bg-card rounded-lg border border-border overflow-hidden">
      <div className="bg-muted/50 px-3 py-1.5">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</p>
        {sub && <p className="text-[11px] text-muted-foreground">{sub}</p>}
      </div>
      {children}
    </div>
  );
}

function GlucoseByMonth({ months }) {
  if (!months?.length) return null;
  const cell = "px-2 py-0.5 text-right tabular-nums";
  return (
    <TableCard title="Glucose by month" sub="Dexcom CGM, each calendar month with readings.">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wide text-muted-foreground">
            <th className="px-3 py-0.5 text-left font-medium">Month</th>
            <th className={`${cell} font-medium`}>Avg</th>
            <th className={`${cell} font-medium`}>GMI</th>
            <th className={`${cell} font-medium`}>In range</th>
            <th className={`${cell} font-medium`}>&lt;70</th>
            <th className={`${cell} font-medium`}>&lt;54</th>
            <th className={`${cell} font-medium`}>&gt;180</th>
            <th className={`${cell} font-medium`}>CV</th>
            <th className={`${cell} font-medium pr-3`}>Low episodes</th>
          </tr>
        </thead>
        <tbody>
          {months.map((m) => (
            <tr key={m.month} className="border-t border-border">
              <td className="px-3 py-0.5 whitespace-nowrap">{monthLabel(m)}</td>
              <td className={cell}>{m.avg}</td>
              <td className={cell}>{fixed(m.gmi)}%</td>
              <td className={`${cell} ${m.tir >= TIR_TARGET ? "text-green-700" : "text-amber-700"}`}>{fixed(m.tir)}%</td>
              <td className={`${cell} ${m.tbr70 > TBR70_TARGET ? "text-red-600 font-semibold" : ""}`}>{fixed(m.tbr70)}%</td>
              <td className={`${cell} ${m.tbr54 > TBR54_TARGET ? "text-red-600 font-semibold" : ""}`}>{fixed(m.tbr54)}%</td>
              <td className={`${cell} ${m.tar180 > TAR180_TARGET ? "text-amber-700 font-semibold" : ""}`}>{fixed(m.tar180)}%</td>
              <td className={`${cell} ${m.cv > CV_TARGET ? "text-amber-700" : ""}`}>{fixed(m.cv)}%</td>
              <td className={`${cell} pr-3`}>{m.low_episodes}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableCard>
  );
}

const WALK_BANDS = [
  ["under_120", "120 or below"],
  ["120_to_170", "121–170"],
  ["over_170", "Above 170"],
];

function fixed(value, digits = 1) {
  return value == null ? "—" : Number(value).toFixed(digits);
}

function signed(value, digits = 0) {
  if (value == null) return "—";
  const text = Math.abs(value).toFixed(digits);
  return value > 0 ? `+${text}` : value < 0 ? `−${text}` : text;
}

function LowTiming({ lows, walking }) {
  const hours = lows?.pct_below_70_by_hour;
  const bands = WALK_BANDS.filter(([key]) => walking?.bands?.[key]?.walking);
  if (!hours?.some((value) => value != null) && !bands.length) return null;
  const data = (hours || []).map((pct, hour) => ({ hour, pct }));
  const middle = walking?.bands?.["120_to_170"]?.walking;
  return (
    <div className="report-card bg-card rounded-lg border border-border p-3 space-y-2">
      <p className="text-xs font-medium">When lows happen</p>
      {hours && (
        <>
          <p className="text-[11px] text-muted-foreground">Share of readings below 70 mg/dL in each hour of the day.</p>
          <div className="h-28">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" vertical={false} />
                <XAxis dataKey="hour" tickFormatter={hourLabel} tick={{ fontSize: 10 }} interval={2} />
                <YAxis tickFormatter={(v) => `${v}%`} tick={{ fontSize: 10 }} width={32} />
                <Tooltip formatter={(v) => [`${v}%`, "below 70"]} labelFormatter={hourLabel} contentStyle={{ fontSize: 12, borderRadius: 8 }} />
                <Bar dataKey="pct" fill={TIR_COLORS.low} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <ul className="text-xs space-y-0.5">
            {lows.span && (
              <li>
                {lows.span.share_pct}% of readings below 70 fell between {hourLabel(lows.span.start_hour)} and {hourLabel(lows.span.end_hour)}.
              </li>
            )}
            {lows.overnight?.nights > 0 && (
              <li>
                Nights with any reading below 70 between {lows.overnight.hours.replace("–", " and ")}: {lows.overnight.nights_with_low} of {lows.overnight.nights}.
              </li>
            )}
          </ul>
        </>
      )}
      {bands.length > 0 && (
        <div className="space-y-1 pt-1">
          <p className="text-xs font-medium">After walking ({walking.walks_measured.toLocaleString()} walks of {walking.median_walk_minutes ? `median ${walking.median_walk_minutes} min` : "8+ min"}; Google Health steps)</p>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-[10px] uppercase tracking-wide text-muted-foreground">
                <th className="py-0.5 text-left font-medium">Glucose at start</th>
                <th className="py-0.5 text-right font-medium">Walks</th>
                <th className="py-0.5 text-right font-medium">Change at 2 h</th>
                <th className="py-0.5 text-right font-medium">Lowest within 2 h</th>
                <th className="py-0.5 text-right font-medium">Reached &lt;70</th>
                <th className="py-0.5 text-right font-medium">Sitting: change / &lt;70</th>
              </tr>
            </thead>
            <tbody>
              {bands.map(([key, label]) => {
                const walk = walking.bands[key].walking;
                const sit = walking.bands[key].sitting;
                return (
                  <tr key={key} className="border-t border-border tabular-nums">
                    <td className="py-0.5">{label}</td>
                    <td className="py-0.5 text-right">{walk.n}</td>
                    <td className="py-0.5 text-right">{signed(walk.delta_120)}</td>
                    <td className="py-0.5 text-right">{signed(walk.nadir_delta)}</td>
                    <td className={`py-0.5 text-right ${walk.went_low_pct >= 25 ? "text-red-600 font-semibold" : ""}`}>{walk.went_low_pct}%</td>
                    <td className="py-0.5 text-right text-muted-foreground">{sit ? `${signed(sit.delta_120)} / ${sit.went_low_pct}%` : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {middle?.at_stop_delta != null && middle?.after_stop_delta != null && (
            <p className="text-xs">
              Walks starting at 121–170: {signed(middle.at_stop_delta)} mg/dL by the end of the walk, then a further {signed(middle.after_stop_delta)} after it ended.
            </p>
          )}
          <p className="text-[11px] text-muted-foreground">
            Changes are medians in mg/dL from the reading at the start. Sitting comparison: half-hour marks with no walking in the next 2 hours, from the same starting range.
          </p>
        </div>
      )}
    </div>
  );
}

function A1cVsGmi({ rows }) {
  if (!rows?.length) return null;
  return (
    <TableCard title="A1c and CGM-estimated GMI" sub="GMI from the CGM readings in the 90 days before each draw.">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wide text-muted-foreground">
            <th className="px-3 py-0.5 text-left font-medium">A1c drawn</th>
            <th className="px-3 py-0.5 text-right font-medium">A1c</th>
            <th className="px-3 py-0.5 text-right font-medium">GMI</th>
            <th className="px-3 py-0.5 text-right font-medium">A1c − GMI</th>
            <th className="px-3 py-0.5 text-right font-medium">CGM days</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.date} className="border-t border-border tabular-nums">
              <td className="px-3 py-0.5">{row.date}</td>
              <td className="px-3 py-0.5 text-right">{fixed(row.a1c)}%</td>
              <td className="px-3 py-0.5 text-right">{fixed(row.gmi)}%</td>
              <td className="px-3 py-0.5 text-right font-semibold">{signed(row.difference, 1)}</td>
              <td className="px-3 py-0.5 text-right text-muted-foreground">{row.cgm_days}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableCard>
  );
}

function PumpUse({ pump, insulin }) {
  if (!pump) return null;
  const modes = pump.modes;
  const temp = pump.temp_basals;
  return (
    <div className="report-card bg-card rounded-lg border border-border p-3 space-y-2">
      <p className="text-xs font-medium">How the pump is used</p>
      {modes && (
        <>
          <div>
            <div className="flex h-4 rounded overflow-hidden border border-border" aria-label="Manual mode share by hour">
              {modes.manual_share_by_hour.map((share, hour) => (
                <div
                  key={hour}
                  className="flex-1 border-r border-white/60 last:border-r-0"
                  style={{ background: `rgba(217, 119, 6, ${0.12 + 0.88 * share / 100})` }}
                  title={`${hourLabel(hour)}: manual ${share}%`}
                />
              ))}
            </div>
            <div className="flex justify-between text-[10px] text-muted-foreground mt-0.5">
              <span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span>
            </div>
          </div>
          <p className="text-xs">
            {modes.manual_span
              ? `Manual mode in most of each hour from ${hourLabel(modes.manual_span.start_hour)} to ${hourLabel(modes.manual_span.end_hour)}; automated mode the rest of the day. `
              : ""}
            Over {modes.days_with_mode_records} days: automated {modes.automated_pct}% of logged pump time, manual {modes.manual_pct}%. Darker hours are mostly manual.
          </p>
        </>
      )}
      <ul className="text-xs space-y-0.5">
        {temp && (
          <li>
            Temporary basal increases: {temp.per_day} per day ({temp.count} in the window), median +{temp.median_increase_pct}%,
            median recorded length {temp.median_minutes} min.
          </li>
        )}
        {insulin?.boluses_per_day != null && <li>Boluses recorded: {insulin.boluses_per_day} per day.</li>}
      </ul>
    </div>
  );
}

function InsulinByMonth({ months, units }) {
  if (!months?.length) return null;
  const carried = months.some((m) => m.weight_carried);
  const cell = "px-2 py-0.5 text-right tabular-nums";
  return (
    <TableCard title="Insulin by month" sub="Pump-reported daily totals (Omnipod 5 via Glooko), averaged over the days on file.">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wide text-muted-foreground">
            <th className="px-3 py-0.5 text-left font-medium">Month</th>
            <th className={`${cell} font-medium`}>Days</th>
            <th className={`${cell} font-medium`}>Total U/day</th>
            <th className={`${cell} font-medium`}>Basal</th>
            <th className={`${cell} font-medium`}>Bolus</th>
            <th className={`${cell} font-medium`}>Weight</th>
            <th className={`${cell} font-medium pr-3`}>U/kg/day</th>
          </tr>
        </thead>
        <tbody>
          {months.map((m) => (
            <tr key={m.month} className="border-t border-border">
              <td className="px-3 py-0.5 whitespace-nowrap">{monthName(m.month)}</td>
              <td className={`${cell} text-muted-foreground`}>{m.days}</td>
              <td className={`${cell} font-semibold`}>{fixed(m.tdd)}</td>
              <td className={cell}>{fixed(m.basal)}</td>
              <td className={cell}>{fixed(m.bolus)}</td>
              <td className={cell}>{m.weight_kg != null ? `${formatWeight(m.weight_kg, units)}${m.weight_carried ? "†" : ""}` : "—"}</td>
              <td className={`${cell} pr-3`}>{fixed(m.units_per_kg, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {carried && <p className="px-3 py-1 text-[10px] text-muted-foreground">† No weight logged that month; the latest earlier weight is used.</p>}
    </TableCard>
  );
}

function ImagingSection({ studies }) {
  if (!studies?.length) return null;
  return (
    <div className="space-y-2">
      <SectionTitle sub="Summaries machine-extracted from the uploaded reports.">Imaging</SectionTitle>
      {studies.map((study) => (
        <div key={study.id} className="report-card bg-card rounded-lg border border-border p-3 space-y-1">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <p className="text-sm font-semibold">{study.title}</p>
            <span className="text-xs text-muted-foreground tabular-nums">{study.date || "date unknown"}</span>
          </div>
          {study.summary && <p className="text-xs leading-relaxed">{study.summary}</p>}
          {study.measurements?.length > 0 && (
            <ul className="text-xs flex flex-wrap gap-x-4 gap-y-0.5">
              {study.measurements.map((m) => (
                <li key={m.name}>
                  {m.name}: <b>{m.value}{m.unit ? ` ${m.unit}` : ""}</b>
                  {m.flag && m.flag !== "normal" && <span className="ml-1 text-[10px] uppercase font-semibold text-red-600">{m.flag}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </div>
  );
}

// Facts that need the clinician's attention, computed from the report data.
function summaryFindings(report) {
  const findings = [];
  const g = report.glucose;
  const unresolved = report.contradictions?.unresolved || [];
  if (unresolved.length) {
    const blocking = unresolved.filter((item) => item.severity === "blocking").length;
    findings.push({
      tone: "red",
      label: "Data conflicts",
      text: `${plural(unresolved.length, "unresolved contradiction")} between data sources${blocking ? ` (${blocking} blocking)` : ""}; both values are listed under Unresolved data contradictions.`,
    });
  }
  if (g.tbr70 > TBR70_TARGET || g.tbr54 > TBR54_TARGET) {
    const lows = g.low_episodes;
    const recentMonths = (report.trends?.glucose_by_month || []).slice(-FINDING_MONTHS_LISTED);
    findings.push({
      tone: "red",
      label: "Hypoglycemia",
      text: `Time below range exceeds the consensus target: ${g.tbr70}% of readings below 70 mg/dL (target <${TBR70_TARGET}%), ${g.tbr54}% below 54 mg/dL (target <${TBR54_TARGET}%).`
        + (lows?.episodes ? ` ${plural(lows.episodes, "low episode")} of 15+ minutes (${lows.per_week}/week), ${lows.nocturnal_episodes} overnight (00:00–06:00), ${lows.level2_episodes} reaching below 54 mg/dL.` : "")
        + (recentMonths.length > 1 ? ` Below 70 by month: ${recentMonths.map((m) => `${MONTH_NAMES[Number(m.month.slice(5)) - 1]} ${m.tbr70}%`).join(", ")}.` : ""),
    });
  }
  const a1c = report.trends?.a1c_vs_gmi || [];
  const direction = a1c.every((row) => row.difference <= -A1C_GMI_GAP) ? "lower" : a1c.every((row) => row.difference >= A1C_GMI_GAP) ? "higher" : null;
  if (a1c.length > 1 && direction) {
    findings.push({
      tone: "amber",
      label: "A1c vs GMI",
      text: `A1c is ${direction} than the CGM-estimated GMI at every draw: ${a1c.map((row) => `${row.a1c}% vs ${row.gmi}% (${row.date})`).join("; ")}.`,
    });
  }
  if (g.tar180 > TAR180_TARGET || g.tar250 > TAR250_TARGET) {
    findings.push({
      tone: "amber",
      label: "Hyperglycemia",
      text: `${g.tar180}% of readings above 180 mg/dL (target <${TAR180_TARGET}%), ${g.tar250}% above 250 mg/dL (target <${TAR250_TARGET}%).`,
    });
  } else if (g.tir < TIR_TARGET) {
    findings.push({ tone: "amber", label: "Time in range", text: `${g.tir}% (target >${TIR_TARGET}%).` });
  }
  if (g.cv > CV_TARGET) {
    findings.push({ tone: "amber", label: "Variability", text: `Coefficient of variation ${g.cv}% (target ≤${CV_TARGET}%).` });
  }
  const weight = report.weight;
  if (weight?.available && significantWeightLoss(weight)) {
    const change = weightTone(weight.peak_change) ? weight.peak_change : weight.window_change;
    findings.push({
      tone: "red",
      label: "Weight loss",
      text: `${formatWeight(weight.latest?.weight_kg, weight.units)} on ${weight.latest?.date}: ${formatWeightChange(change, weight.units)} from ${formatWeight(change.from_kg, weight.units)} on ${change.from_date}${change === weight.peak_change ? " (12-month high)" : ""}.`,
    });
  }
  const abnormal = report.labs?.flagged || [];
  if (abnormal.length) {
    const recent = abnormal.filter((entry) => entry.date).slice(0, FINDING_LABS_LISTED);
    findings.push({
      tone: "red",
      label: "Labs",
      text: `${plural(abnormal.length, "result")} outside the reference range at the latest draw.`
        + (recent.length ? ` Most recent: ${recent.map((entry) => `${entry.test_name} ${labResult(entry)} (${entry.flag}, ${entry.date})`).join("; ")}.` : ""),
    });
  }
  const symptoms = (report.history?.events || []).filter((event) => REPORTED_SYMPTOMS.test(event.title || ""));
  if (symptoms.length) {
    const dates = [...new Set(symptoms.map((event) => event.date).filter(Boolean))];
    // A topic ("Teeth") says nothing on its own; each entry's opening
    // sentence states the symptom in the patient's words.
    findings.push({
      tone: "amber",
      label: "Reported symptoms",
      text: dates.length ? `Recorded ${dates.join(", ")}.` : "",
      items: symptoms.map((event) => ({ topic: symptomTitle(event.title), text: firstSentence(event.details) })),
    });
  }
  const effort = report.management_burden;
  if (effort?.outcome_vs_effort?.sustainability_review_flag && effort.summary) {
    findings.push({
      tone: "amber",
      label: "Management effort",
      text: `${effort.summary.average_active_management_minutes_per_day} min/day of recorded diabetes management`
        + `${effort.summary.measured_interactions_per_day != null ? ` and ${effort.summary.measured_interactions_per_day} interactions/day` : ""}`
        + `${effort.summary.days_with_measured_effort ? ` over ${effort.summary.days_with_measured_effort} days` : ""}.`,
    });
  }
  return findings;
}

function Findings({ findings }) {
  if (!findings.length) {
    return <p className="text-sm text-muted-foreground">No glucose, weight or lab result in this report crossed a consensus target or reference range.</p>;
  }
  return (
    <ul className="space-y-1.5 text-sm">
      {findings.map((finding) => (
        <li key={finding.label} className="flex gap-2">
          <span className={`mt-1.5 inline-block w-2 h-2 shrink-0 rounded-full ${finding.tone === "red" ? "bg-red-600" : "bg-amber-500"}`} />
          <div className="min-w-0">
            <span><b>{finding.label}.</b> {finding.text}</span>
            {finding.items?.length > 0 && (
              <ul className="mt-1 columns-1 md:columns-2 print:columns-2 gap-5 text-xs leading-snug">
                {finding.items.map((item) => (
                  <li key={item.topic} className="break-inside-avoid mb-0.5"><span className="font-semibold">{item.topic}:</span> {item.text}</li>
                ))}
              </ul>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

function RecordColumn({ title, children, empty }) {
  return (
    <div className="report-card rounded-lg border border-border p-3">
      <h3 className="font-semibold text-sm mb-1">{title}</h3>
      {children || <p className="text-xs text-muted-foreground">{empty}</p>}
    </div>
  );
}

function ClinicalRecord({ report }) {
  const conditions = report.conditions || [];
  const medications = report.medications || [];
  const allergies = report.allergies || [];
  return (
    <div className="report-section grid grid-cols-1 sm:grid-cols-3 print:grid-cols-3 gap-3">
      <RecordColumn title="Confirmed diagnoses" empty="None recorded.">
        {conditions.length > 0 && (
          <ul className="text-xs space-y-0.5">
            {conditions.map((c, i) => (
              <li key={i}>
                <span className="font-medium">{c.name}</span>
                {c.status && c.status !== "active" ? ` (${c.status})` : ""}
                {c.diagnosed ? <span className="text-muted-foreground"> · dx {c.diagnosed}</span> : ""}
              </li>
            ))}
          </ul>
        )}
      </RecordColumn>
      <RecordColumn title="Medications & supplements" empty="None recorded.">
        {medications.length > 0 && (
          <ul className="text-xs space-y-0.5">
            {medications.map((m, i) => (
              <li key={i}>
                <span className="font-medium">{m.name}</span>{m.dose ? ` ${m.dose}` : ""}{m.frequency ? ` · ${m.frequency}` : ""}
                {m.kind === "supplement" ? <span className="text-muted-foreground"> (supplement)</span> : ""}{m.status === "stopped" ? " — stopped" : ""}
              </li>
            ))}
          </ul>
        )}
      </RecordColumn>
      <RecordColumn title="Allergies & intolerances" empty="None recorded.">
        {allergies.length > 0 && (
          <ul className="text-xs space-y-0.5">
            {allergies.map((a, i) => (
              <li key={i}>
                <span className="font-medium">{a.allergen}</span>{a.severity ? ` (${a.severity})` : ""}{a.reaction ? ` — ${a.reaction}` : ""}
              </li>
            ))}
          </ul>
        )}
      </RecordColumn>
    </div>
  );
}

// Sentences end at punctuation followed by a space, so "8.5" stays whole.
function firstSentence(text) {
  return String(text || "").trim().split(/(?<=[.!?])\s+/)[0];
}

function symptomTitle(title) {
  const text = title.replace(REPORTED_SYMPTOMS, "");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function HistorySection({ history }) {
  const events = history?.events || [];
  const reported = events.filter((event) => REPORTED_SYMPTOMS.test(event.title || ""));
  const other = events.filter((event) => !REPORTED_SYMPTOMS.test(event.title || ""));
  if (!reported.length && !other.length && !history?.narrative) return null;
  const dates = [...new Set(reported.map((event) => event.date).filter(Boolean))];
  return (
    <div className="space-y-3">
      {reported.length > 0 && (
        <div className="space-y-2">
          <SectionTitle sub={dates.length ? `Recorded ${dates.join(", ")}.` : null}>Patient-reported symptoms</SectionTitle>
          <dl className="columns-1 md:columns-2 print:columns-2 gap-6 text-xs leading-relaxed">
            {reported.map((event, i) => (
              <div key={i} className="break-inside-avoid mb-2">
                <dt className="font-semibold">{symptomTitle(event.title)}</dt>
                <dd>{event.details}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
      {(history?.narrative || other.length > 0) && (
        <div className="space-y-1">
          <h3 className="font-semibold text-sm">Health history</h3>
          {history?.narrative && <p className="text-xs leading-relaxed whitespace-pre-wrap">{history.narrative}</p>}
          {other.length > 0 && (
            <ul className="text-xs space-y-1">
              {other.map((e, i) => (
                <li key={i}>
                  <span className="text-muted-foreground tabular-nums">{e.date || "—"} · <span className="capitalize">{e.type}</span></span>{" "}
                  <span className="font-medium">{e.title}</span>
                  {e.details && <span className="text-muted-foreground"> — {e.details}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function HealthEpisodes({ block }) {
  if (!block?.episodes?.length && !block?.medication_exposures?.length) return null;
  return (
    <div className="space-y-2">
      <SectionTitle sub={block.semantics}>Health episodes &amp; medication exposures</SectionTitle>
      {block.episodes?.length > 0 && (
        <ul className="space-y-1 text-xs">
          {block.episodes.map((episode) => (
            <li key={episode.id}>
              <span className="font-semibold">{episode.title}</span>
              {" · "}{episode.start_time} → {episode.end_time}
              {" · "}{episode.status} · {episode.origin_kind}
              <span className="text-muted-foreground">
                {" · "}{episode.members?.length || 0} temporal members · confidence{" "}
                {episode.confidence?.confidence_label?.replace("_", " ") || "not assessed"}
              </span>
            </li>
          ))}
        </ul>
      )}
      {block.medication_exposures?.length > 0 && (
        <ul className="space-y-1 text-xs">
          {block.medication_exposures.map((exposure) => (
            <li key={exposure.id}>
              <span className="font-semibold">{exposure.medication_name}</span>
              {exposure.dose ? ` · ${exposure.dose}` : ""}
              {exposure.formulation ? ` · ${exposure.formulation}` : ""}
              {" · "}{exposure.start_time} → {exposure.end_time || "ongoing"}
              {" · "}{exposure.status}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const EFFORT_LABELS = {
  temp_basal: "Temp basal changes (all)",
  awakening: "Overnight awakenings",
  device_change: "Device changes",
};

function ManagementEffort({ mb }) {
  if (!mb?.summary) return null;
  return (
    <div className="space-y-2">
      <SectionTitle sub="Control and recorded effort are separate dimensions. Missing event sources reduce confidence and are not counted as zero effort. These calculations are descriptive and do not establish causation or recommend treatment.">
        Outcomes &amp; management effort
      </SectionTitle>
      <p className="text-xs">
        Time in range {mb.outcomes?.time_in_range_pct != null ? `${mb.outcomes.time_in_range_pct}%` : "—"} ·
        {" "}{mb.summary.average_active_management_minutes_per_day} recorded min/day of active management ·
        {" "}effort index {mb.summary.measured_effort_index} · effort confidence {Math.round(mb.analytics_confidence.confidence_score * 100)}% ({mb.analytics_confidence.confidence_label})
      </p>
      {mb.outcome_vs_effort?.sustainability_review_flag && (
        <p className="rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-950">{mb.outcome_vs_effort.language}</p>
      )}
      <table className="w-full text-xs">
        <tbody>
          {mb.components.map((item) => (
            <tr key={item.category} className="border-t border-border">
              <td className="py-0.5 pr-2 capitalize">{EFFORT_LABELS[item.category] || item.category.replaceAll("_", " ")}</td>
              <td className="py-0.5 pr-2 text-right tabular-nums">{plural(item.events, "event")}</td>
              <td className="py-0.5 pr-2 text-right tabular-nums">{item.minutes} min</td>
              <td className="py-0.5 text-right tabular-nums text-muted-foreground">weight {item.weight} · {item.weighted_points} points</td>
            </tr>
          ))}
        </tbody>
      </table>
      {mb.source_coverage?.missing?.length > 0 && (
        <p className="text-[11px] text-amber-700">
          Unavailable event sources: {mb.source_coverage.missing.join(", ").replaceAll("_", " ")}.
        </p>
      )}
    </div>
  );
}

function InsulinResponse({ ir }) {
  if (!(ir?.counts?.total > 0)) return null;
  return (
    <div className="space-y-2">
      <SectionTitle>Observed insulin response events</SectionTitle>
      <p className="text-xs">
        {ir.counts.total} response windows over {ir.window_days} days: {ir.counts.clean} clean (used in summaries),
        {" "}{ir.counts.confounded} confounded, {ir.counts.excluded} excluded for insufficient or invalid input.
      </p>
      {ir.available ? (
        <p className="text-xs">
          Observed drop per unit: median <b>{ir.median_drop_per_unit} mg/dL/U</b>, mean {ir.mean_drop_per_unit} mg/dL/U,
          {" "}variability {ir.cv_pct}% · {ir.n} clean events · {ir.confidence?.confidence_label || "low"} confidence
          {ir.confidence?.discovery_status ? ` (${ir.confidence.discovery_status})` : ""}.
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">{ir.reason}</p>
      )}
      {Object.keys(ir.reason_counts || {}).length > 0 && (
        <p className="text-[11px] text-muted-foreground">
          Exclusion/confounder reasons: {Object.entries(ir.reason_counts).map(([reason, count]) => `${reason.replaceAll("_", " ")} (${count})`).join(" · ")}
        </p>
      )}
      <p className="text-[11px] text-muted-foreground">
        Confounded events are excluded from summaries. The observed glucose change does not establish insulin causation, resistance, or absorption; estimated IOB is a comparison assumption, not pump-reported IOB.
      </p>
    </div>
  );
}

function ActivityAssociations({ ap }) {
  if (!ap?.effects?.length) return null;
  return (
    <div className="space-y-2">
      <SectionTitle sub="Timestamped interval comparisons only. They do not establish that activity or position caused a glucose, bolus-response, or meter/CGM difference.">
        Activity &amp; position associations
      </SectionTitle>
      <ul className="text-xs space-y-1">
        {ap.effects.map((effect) => (
          <li key={effect.id}>
            <span className="font-semibold capitalize">{effect.dimension}: {effect.state}</span>
            {" — "}{effect.metric.replaceAll("_", " ")}: <b>{Math.round(effect.observed_mean * 10) / 10} {effect.unit}</b>
            <span className="text-muted-foreground">
              {" "}(n={effect.sample_count}; {effect.measured_interval_count}/{effect.interval_count} intervals measured · {effect.analytics_confidence.discovery_status.replaceAll("_", " ")})
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function CycleTable({ c }) {
  return (
    <div className="report-card bg-card rounded-lg border border-border p-3 space-y-2">
      <h3 className="font-semibold text-sm">Menstrual cycle &amp; glucose</h3>
      <p className="text-[11px] text-muted-foreground">
        {plural(c.cycles_detected, "cycle")}{c.avg_cycle_length ? `, average ${c.avg_cycle_length} days` : ""} · phases {c.source}.
      </p>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wide text-muted-foreground">
            <th className="text-left font-medium py-0.5">Phase</th>
            <th className="text-right font-medium py-0.5">Days</th>
            <th className="text-right font-medium py-0.5">In range</th>
            <th className="text-right font-medium py-0.5">Avg glucose</th>
          </tr>
        </thead>
        <tbody>
          {["menstrual", "follicular", "ovulation", "luteal"].filter((p) => c.per_phase[p]).map((p) => {
            const s = c.per_phase[p];
            return (
              <tr key={p} className="border-t border-border">
                <td className="py-0.5 capitalize">{p}</td>
                <td className="py-0.5 text-right tabular-nums">{s.days}</td>
                <td className="py-0.5 text-right tabular-nums">{s.tir != null ? `${s.tir}%` : "—"}</td>
                <td className="py-0.5 text-right tabular-nums">{s.avg_glucose != null ? `${s.avg_glucose} mg/dL` : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function WellnessTable({ w }) {
  const rows = [
    ["Sleep score (Oura)", w.oura?.avg_sleep_score],
    ["Readiness (Oura)", w.oura?.avg_readiness_score],
    ["Resting heart rate (Oura)", w.oura?.avg_resting_hr != null ? `${w.oura.avg_resting_hr} bpm` : null],
    ["SpO₂ (Oura)", w.oura?.avg_spo2 != null ? `${w.oura.avg_spo2}%` : null],
    ["Steps per day (Fitbit)", w.fitbit?.avg_steps?.toLocaleString()],
    ["Sleep per night (Fitbit)", w.fitbit?.avg_sleep_hours != null ? `${w.fitbit.avg_sleep_hours} h` : null],
    ["Resting heart rate (Fitbit)", w.fitbit?.avg_resting_hr != null ? `${w.fitbit.avg_resting_hr} bpm` : null],
  ].filter(([, value]) => value != null);
  return (
    <div className="report-card bg-card rounded-lg border border-border p-3 space-y-2">
      <h3 className="font-semibold text-sm">Sleep, recovery &amp; activity <span className="font-normal text-[11px] text-muted-foreground">(averages)</span></h3>
      <table className="w-full text-xs">
        <tbody>
          {rows.map(([label, value]) => (
            <tr key={label} className="border-t border-border">
              <td className="py-0.5">{label}</td>
              <td className="py-0.5 text-right tabular-nums font-medium">{value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function InsuranceBlock({ insurance }) {
  return (
    <div className="report-section report-card rounded-lg border border-border p-3">
      <h2 className="font-semibold text-sm mb-2 flex items-center gap-2">
        <ShieldCheck className="w-4 h-4 text-primary" /> Health insurance
      </h2>
      <div className="grid grid-cols-2 sm:grid-cols-5 print:grid-cols-5 gap-x-4 gap-y-1 text-xs">
        {[
          ["Carrier", insurance.carrier],
          ["Plan", [insurance.plan_name, insurance.plan_type].filter(Boolean).join(" · ")],
          ["Member", insurance.member_name],
          ["Member ID", insurance.member_id],
          ["Group", insurance.group_number],
          ["Member services", insurance.customer_service_phone],
          ["RxBIN", insurance.rx_bin],
          ["RxPCN", insurance.rx_pcn],
          ["RxGroup", insurance.rx_group],
          ["Effective", insurance.effective_date],
        ]
          .filter(([, v]) => v)
          .map(([label, v]) => (
            <div key={label}>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
              <div className="font-medium tabular-nums">{v}</div>
            </div>
          ))}
      </div>
      {insurance.notes && <p className="text-[11px] text-muted-foreground mt-2">{insurance.notes}</p>}
    </div>
  );
}

export default function Report() {
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(90);
  const [showUnevaluated, setShowUnevaluated] = useState(false);
  const [includeUnevaluatedInPrint, setIncludeUnevaluatedInPrint] = useState(false);
  const [printAllLabs, setPrintAllLabs] = useState(false);
  const [printAnalyses, setPrintAnalyses] = useState(false);

  const generate = useCallback(async (d) => {
    setLoading(true);
    try {
      const res = await fetch("/api/report/visit", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ days: d }),
      });
      setReport(res.ok ? await res.json() : null);
    } catch {
      setReport(null);
    }
    setLoading(false);
  }, []);

  useEffect(() => { generate(days); }, [days, generate]);

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-3 text-muted-foreground">
        <Loader2 className="w-6 h-6 animate-spin text-primary" />
        <p className="text-sm">Compiling the report and writing the summary…</p>
      </div>
    );
  }
  if (!report?.glucose) return <p className="text-sm text-muted-foreground">Could not generate the report.</p>;

  const g = report.glucose;
  const i = report.insulin;
  const ir = report.insulin_response;
  const ap = report.activity_position;
  const mb = report.management_burden;
  const c = report.cycle;
  const w = report.wellness;
  const labs = report.labs;
  const n = report.narrative;
  const hypotheses = report.hypotheses || [];
  const unevaluatedHypotheses = hypotheses.filter((item) => (
    item.legacy || hypothesisState(item) === "not_evaluated"
  ));
  const reviewedHypotheses = hypotheses.filter((item) => (
    !item.legacy && hypothesisState(item) !== "not_evaluated"
  ));
  const findings = summaryFindings(report);
  const latestKg = report.weight?.available ? report.weight.latest?.weight_kg : null;
  const tdd = i.pump_reported_avg_tdd ?? i.calculated_avg_tdd;
  const labCounts = labs.verification?.counts || {};
  const verifiedLabs = (labCounts.approved || 0) + (labCounts.edited || 0);
  const perRowVerification = verifiedLabs > 0 && (labCounts.unverified || 0) > 0;
  const labTests = Object.values(labs.categories || {}).reduce((sum, tests) => sum + tests.length, 0);
  const labPanels = Object.fromEntries(
    Object.entries(labs.categories || {}).flatMap(([cat, tests]) => tests.map((t) => [t.test_name, cat])),
  );
  const hasAnalyses = (report.health_episodes?.episodes?.length || report.health_episodes?.medication_exposures?.length)
    || mb?.summary || ir?.counts?.total > 0 || ap?.effects?.length > 0;

  return (
    <div className="space-y-5 max-w-4xl mx-auto">
      <style>{PRINT_CSS}</style>

      {/* Controls (hidden in print) */}
      <div className="flex items-center justify-between flex-wrap gap-3 print:hidden">
        <div className="flex items-center gap-2">
          {[30, 90, 180].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                days === d ? "bg-primary text-primary-foreground border-primary" : "bg-secondary border-border hover:bg-accent"
              }`}
            >
              {d} days
            </button>
          ))}
          <Button variant="outline" size="sm" onClick={() => generate(days)} className="gap-2">
            <RefreshCw className="w-3.5 h-3.5" /> Regenerate
          </Button>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <label className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <input type="checkbox" checked={printAllLabs} onChange={(event) => setPrintAllLabs(event.target.checked)} />
            Print all lab results
          </label>
          <label className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <input type="checkbox" checked={printAnalyses} onChange={(event) => setPrintAnalyses(event.target.checked)} />
            Print additional analyses
          </label>
          <Button onClick={() => window.print()} className="gap-2">
            <Printer className="w-4 h-4" /> Print / Save PDF
          </Button>
        </div>
      </div>

      {/* Report header */}
      <div className="report-section flex items-start justify-between border-b border-border pb-3">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <FileText className="w-6 h-6 text-primary" /> Health Summary
          </h1>
          <PatientLine patient={report.patient} />
          <p className="text-sm text-muted-foreground mt-1">
            {report.days}-day review · {report.start_date} to {report.end_date}
          </p>
          <DataSourcesLine report={report} />
        </div>
        <div className="text-right text-xs text-muted-foreground">
          <div className="w-9 h-9 rounded-lg bg-primary flex items-center justify-center ml-auto mb-1">
            <span className="text-primary-foreground font-mono font-bold text-sm">GP</span>
          </div>
          Generated {new Date(report.generated_at).toLocaleDateString()}
        </div>
      </div>

      {/* Page 1: the numbers, what needs attention, and the clinical record */}
      <div className="report-section grid grid-cols-2 md:grid-cols-6 print:grid-cols-6 gap-2">
        <Metric label="Time in range" value={`${g.tir}%`} sub={`70–180 · target >${TIR_TARGET}%`} tone={g.tir >= TIR_TARGET ? "text-green-600" : "text-amber-600"} />
        <Metric
          label="Time below 70 mg/dL"
          value={`${g.tbr70}%`}
          sub={`target <${TBR70_TARGET}% · ${g.tbr70 > TBR70_TARGET ? "above target" : "within target"}`}
          tone={g.tbr70 > TBR70_TARGET ? "text-red-600" : "text-green-600"}
        />
        <Metric
          label="Time below 54 mg/dL"
          value={`${g.tbr54}%`}
          sub={`target <${TBR54_TARGET}% · ${g.tbr54 > TBR54_TARGET ? "above target" : "within target"}`}
          tone={g.tbr54 > TBR54_TARGET ? "text-red-600" : "text-green-600"}
        />
        <Metric label="Average glucose" value={`${g.avg}`} sub={`mg/dL · GMI ${g.gmi}% (est. A1c)`} />
        <Metric label="Variability (CV)" value={`${g.cv}%`} sub={g.cv <= CV_TARGET ? `target ≤${CV_TARGET}%` : `above ${CV_TARGET}% target`} tone={g.cv <= CV_TARGET ? "" : "text-amber-600"} />
        <Metric
          label="Total daily insulin"
          value={tdd != null ? `${tdd}` : "—"}
          sub={tdd != null ? `U/day${latestKg ? ` · ${(tdd / latestKg).toFixed(2)} U/kg` : ""}` : "no complete days"}
        />
      </div>

      <div className="report-section space-y-2">
        <SectionTitle>Findings to review</SectionTitle>
        <Findings findings={findings} />
      </div>

      {n?.discussion_points?.length > 0 && (
        <div className="report-section report-card bg-primary/5 rounded-lg border border-primary/20 p-3">
          <h2 className="font-semibold text-sm text-primary mb-1">Points to discuss</h2>
          <ol className="text-sm space-y-1 list-decimal list-inside marker:text-primary">
            {n.discussion_points.map((p, idx) => <li key={idx}>{p}</li>)}
          </ol>
        </div>
      )}

      {!n && report.narrative_status?.available === false && (
        <div className="report-section print:hidden rounded-lg border border-dashed border-amber-300 bg-amber-50 px-4 py-3 text-xs text-amber-900">
          <p className="font-semibold">Narrative unavailable</p>
          <p>{report.narrative_status.reason}</p>
          {report.narrative_status.note && <p className="text-amber-800">{report.narrative_status.note}</p>}
        </div>
      )}

      <ClinicalRecord report={report} />

      {report.contradictions?.unresolved?.length > 0 && (
        <div className="report-section space-y-2">
          <h2 className="font-semibold text-base flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-600" /> Unresolved data contradictions
          </h2>
          <p className="text-xs text-muted-foreground">Both sides are shown. No conflicting value has been selected silently.</p>
          {report.contradictions.unresolved.map((item) => (
            <div key={item.id} className={`report-card rounded-lg border p-3 ${item.severity === "blocking" ? "border-red-300 bg-red-50" : "border-amber-200 bg-amber-50"}`}>
              <div className="flex items-center justify-between gap-2">
                <div className="text-sm font-semibold">{item.explanation}</div>
                <span className="text-[9px] uppercase font-semibold">{item.severity}</span>
              </div>
              <div className="grid gap-2 sm:grid-cols-2 mt-2">
                <ContradictionSide side={item.left} />
                <ContradictionSide side={item.right} />
              </div>
              {item.detection_state === "not_current" && (
                <div className="text-[10px] text-muted-foreground mt-2">The latest evaluation no longer detects this condition, but it remains unresolved until explicitly reviewed.</div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Glucose: the AGP page */}
      <div className="report-page-break space-y-3">
        <SectionTitle sub={`${g.readings.toLocaleString()} CGM readings over ${g.days} days.`}>Glucose</SectionTitle>
        {n && (
          <div className="report-card rounded-lg border border-border p-3 space-y-1.5">
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Written summary</p>
            <p className="text-sm font-medium">{n.headline}</p>
            {[n.glucose_summary, n.cycle_summary, n.lifestyle_summary].filter(Boolean).map((text) => (
              <p key={text} className="text-xs text-muted-foreground">{text}</p>
            ))}
          </div>
        )}
        <div className="report-card bg-card rounded-lg border border-border p-3">
          <p className="text-xs font-medium mb-2">Time in ranges</p>
          <TIRBar g={g} />
        </div>
        {g.low_episodes && (
          <div className="grid grid-cols-2 md:grid-cols-4 print:grid-cols-4 gap-2">
            <Metric label="Low episodes (15+ min <70)" value={g.low_episodes.episodes} sub={`${g.low_episodes.per_week} per week`} tone={g.low_episodes.episodes ? "text-red-600" : ""} />
            <Metric label="Overnight (00:00–06:00)" value={g.low_episodes.nocturnal_episodes} sub="episodes" />
            <Metric label="Reaching below 54" value={g.low_episodes.level2_episodes} sub="episodes" tone={g.low_episodes.level2_episodes ? "text-red-600" : ""} />
            <Metric label="Above 180 / above 250" value={`${g.tar180}% / ${g.tar250}%`} sub={`targets <${TAR180_TARGET}% / <${TAR250_TARGET}%`} />
          </div>
        )}
        <div className="report-card bg-card rounded-lg border border-border p-3">
          <p className="text-xs font-medium">Ambulatory Glucose Profile (typical day)</p>
          <p className="text-[11px] text-muted-foreground mb-2">Median (line) with 25–75% and 5–95% bands by time of day. Green dashes mark the 70–180 target.</p>
          <AGPChart agp={g.agp} />
        </div>
        <GlucoseByMonth months={report.trends?.glucose_by_month} />
        <LowTiming lows={report.trends?.lows_by_hour} walking={ap?.walking_response} />
        <A1cVsGmi rows={report.trends?.a1c_vs_gmi} />
        <FingerstickReconciliation g={g} />
      </div>

      <WeightBlock weight={report.weight} />

      {/* Insulin */}
      {i.available && (
        <div className="space-y-3">
          <SectionTitle>Insulin & carbs</SectionTitle>
          <div className="grid grid-cols-2 md:grid-cols-4 print:grid-cols-4 gap-2">
            {i.pump_reported_avg_tdd != null && (
              <Metric label="Pump-reported TDD" value={`${i.pump_reported_avg_tdd}`} sub={`U/day · ${i.pump_reported_days} complete day${i.pump_reported_days === 1 ? "" : "s"} · ${Object.keys(i.pump_reported_sources || {}).join(", ")}`} />
            )}
            {i.calculated_avg_tdd != null && (
              <Metric label="Calculated TDD" value={`${i.calculated_avg_tdd}`} sub={`U/day · ${i.calculated_days} full-coverage day${i.calculated_days === 1 ? "" : "s"}`} />
            )}
            {!i.bolus_stream_incomplete && (
              <Metric label="Bolus insulin" value={`${i.avg_daily_bolus}`} sub={`units/day · ${i.boluses_per_day}/day · ${i.bolus_source || "bolus events"}`} />
            )}
            {i.temp_basal_corrections?.count > 0 && (
              <Metric
                label="Temp-basal corrections"
                value={`${i.temp_basal_corrections.extra_units_per_day}`}
                sub={`units/day above programmed rate · ${i.temp_basal_corrections.per_day}/day · ${i.temp_basal_corrections.source}`}
              />
            )}
            {i.calculated_avg_daily_basal != null && <Metric label="Delivered basal (calc.)" value={`${i.calculated_avg_daily_basal}`} sub="units/day · full coverage only" />}
            {i.scheduled_avg_daily_basal != null && <Metric label="Programmed basal schedule" value={`${i.scheduled_avg_daily_basal}`} sub="units/day · pump settings, not delivered amount" />}
            {i.carbs_representative !== false && <Metric label="Carbs logged" value={`${i.avg_daily_carbs}`} sub="g/day · logged entries only" />}
          </div>
          <PumpUse pump={report.trends?.pump_use} insulin={i} />
          <InsulinByMonth months={report.trends?.insulin_by_month} units={report.weight?.units} />
          <div className="space-y-1 text-[11px] text-muted-foreground">
            {i.bolus_stream_incomplete && (
              <p className="text-amber-800">
                Bolus/basal split not available: Glooko&apos;s event streams do not itemize automated-mode delivery and its daily
                totals record bolus as 0, so the {i.avg_daily_bolus} units/day of recorded boluses (against a total daily dose of
                {" "}{i.avg_tdd_est} units/day) is a data gap, not a record of missed doses.
              </p>
            )}
            {i.carbs_representative === false && (
              <p className="text-amber-800">
                Carbs were logged on {i.carb_logged_days ?? 0} of {i.carb_expected_days ?? "the"} days; a daily carb average is not shown
                because it would understate intake.
              </p>
            )}
            {(i.latest_complete_date || i.limitations?.length > 0) && (
              <p>
                {i.latest_complete_date ? `Complete daily totals through ${i.latest_complete_date}. ` : ""}
                {(i.limitations || []).join(" ")}
              </p>
            )}
            {i.incomplete_days > 0 && (
              <p className="text-amber-700">{i.incomplete_days} day{i.incomplete_days === 1 ? "" : "s"} with insulin activity lacked a complete TDD and were excluded.</p>
            )}
            {i.discrepancy_days > 0 && (
              <p className="text-amber-700">{i.discrepancy_days} day{i.discrepancy_days === 1 ? "" : "s"} had a difference greater than rounding between pump-reported and calculated TDD; both values remain separate.</p>
            )}
          </div>
        </div>
      )}

      {report.pump_settings && <PumpSettingsSection ps={report.pump_settings} />}

      {/* What the patient reports, and open questions */}
      <div className="space-y-5">
        <HistorySection history={report.history} />

        {report.care_notes?.length > 0 && (
          <div className="report-section report-card rounded-lg border border-border p-3">
            <h2 className="font-semibold text-sm mb-2 flex items-center gap-2">
              <ClipboardList className="w-4 h-4 text-primary" /> Care team notes
            </h2>
            <div className="space-y-3">
              {report.care_notes.map((note, idx) => (
                <div key={idx}>
                  <p className="text-sm font-medium">
                    <span className="uppercase text-[10px] tracking-wide text-muted-foreground mr-2">{note.kind}</span>
                    {note.title}
                  </p>
                  {note.body && <p className="text-sm whitespace-pre-wrap">{note.body}</p>}
                  <p className="text-[11px] text-muted-foreground">{note.author}{note.updated ? ` · ${note.updated}` : ""}</p>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Evidence-bearing hypotheses remain separate from diagnoses. */}
        {reviewedHypotheses.length > 0 && (
          <div className="report-section space-y-2">
            <h2 className="font-semibold text-base flex items-center gap-2">
              <Beaker className="w-4 h-4 text-primary" /> Questions under review
            </h2>
            <p className="text-[11px] text-muted-foreground">
              Tentative interpretations with recorded evidence. They remain separate from confirmed diagnoses.
            </p>
            {reviewedHypotheses.map((hypothesis) => {
              const state = HYPOTHESIS_STATE[hypothesisState(hypothesis)];
              const supporting = hypothesis.evidence_by_role?.supporting?.length || 0;
              const opposing = hypothesis.evidence_by_role?.opposing?.length || 0;
              const missing = hypothesis.evidence_by_role?.missing?.length || 0;
              return (
                <div key={hypothesis.id} className="report-card rounded-lg border border-border bg-card p-3 space-y-2">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <p className="font-medium text-sm">{hypothesis.title}</p>
                      {hypothesis.description && <p className="text-xs text-muted-foreground">{hypothesis.description}</p>}
                    </div>
                    <span className={`text-[10px] rounded-full border px-2 py-1 font-medium ${state.tone}`}>
                      {state.label}
                    </span>
                  </div>
                  <p className="text-[11px] text-muted-foreground">
                    Recorded evidence: {supporting} supporting · {opposing} opposing · {missing} missing/needed
                  </p>
                  <EvidenceDetails hypothesis={hypothesis} />
                  {hypothesis.suggested_verification && (
                    <p className="text-xs">
                      <span className="font-semibold">Suggested verification:</span>{" "}
                      {hypothesis.suggested_verification}
                    </p>
                  )}
                  {hypothesis.decided_by && (
                    <p className="text-xs font-semibold">
                      Decision recorded by {hypothesis.decided_by} at {hypothesis.decided_at}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {/* Unsupported legacy suspicions are optional discussion reminders, not findings. */}
        {unevaluatedHypotheses.length > 0 && (
          <div className={`report-section rounded-lg border border-dashed border-border bg-muted/20 p-3 ${includeUnevaluatedInPrint ? "" : "print:hidden"}`}>
            <div className="print:hidden flex flex-wrap items-center justify-between gap-3">
              <button
                type="button"
                onClick={() => setShowUnevaluated((value) => !value)}
                className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground"
              >
                {showUnevaluated ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                Questions previously recorded ({unevaluatedHypotheses.length})
              </button>
              <label className="inline-flex items-center gap-2 text-[11px] text-muted-foreground">
                <input
                  type="checkbox"
                  checked={includeUnevaluatedInPrint}
                  onChange={(event) => setIncludeUnevaluatedInPrint(event.target.checked)}
                />
                Include in print
              </label>
            </div>
            <h2 className="hidden print:block text-sm font-medium">Questions previously recorded</h2>
            {(showUnevaluated || includeUnevaluatedInPrint) && (
              <div className="mt-3 print:mt-2 space-y-2">
                <p className="text-[11px] text-muted-foreground">
                  These entries have no recorded supporting or opposing evidence. They are reminders for discussion, not findings.
                </p>
                {unevaluatedHypotheses.map((hypothesis) => (
                  <div key={hypothesis.id} className="rounded border border-border bg-background/70 px-3 py-2">
                    <p className="text-sm text-foreground">{hypothesis.title}</p>
                    <p className="text-[11px] text-muted-foreground">
                      Not evaluated · no supporting or opposing evidence recorded.
                    </p>
                    {hypothesis.suggested_verification && (
                      <p className="text-[11px] text-muted-foreground mt-1">
                        Possible next step: {hypothesis.suggested_verification}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {(c.available || w.oura || w.fitbit) && (
          <div className="report-section grid grid-cols-1 md:grid-cols-2 print:grid-cols-2 gap-3">
            {c.available && <CycleTable c={c} />}
            {(w.oura || w.fitbit) && <WellnessTable w={w} />}
          </div>
        )}
      </div>

      {/* Labs */}
      {labs.available && (
        <div className="space-y-3">
          <SectionTitle
            sub={(labCounts.unverified || 0) > 0
              ? `${verifiedLabs ? `${labCounts.unverified} of ${labCounts.unverified + verifiedLabs}` : "All"} lab values on file are machine-extracted from uploaded documents and not yet verified against the source.${(labCounts.invalid || 0) > 0 ? ` ${labCounts.invalid} invalid results excluded.` : ""}`
              : null}
          >
            Labs
          </SectionTitle>
          <KeyLabResults results={labs.key_results} perRow={perRowVerification} />
          <OutOfRangeLabs results={labs.flagged} panels={labPanels} perRow={perRowVerification} />
          <OptionalSection
            title={`All lab results (${plural(labTests, "test")})`}
            summary={printAllLabs ? "included in print" : "not printed"}
            includeInPrint={printAllLabs}
          >
            {(labs.duplicates_removed > 0 || labs.undated_results > 0) && (
              <p className="text-[11px] text-muted-foreground">
                {labs.duplicates_removed > 0 ? `${labs.duplicates_removed} duplicate result${labs.duplicates_removed === 1 ? "" : "s"} (same test, date, and value) merged. ` : ""}
                {labs.undated_results > 0 ? `${labs.undated_results} result${labs.undated_results === 1 ? " has" : "s have"} no readable collection date. ` : ""}
                Common test-name variants are combined under one name.
              </p>
            )}
            <AllLabResults categories={labs.categories} perRow={perRowVerification} />
          </OptionalSection>
        </div>
      )}

      <ImagingSection studies={report.imaging} />

      {hasAnalyses && (
        <OptionalSection
          title="Additional analyses"
          summary={printAnalyses ? "included in print" : "management effort, insulin response, activity · not printed"}
          includeInPrint={printAnalyses}
        >
          <ManagementEffort mb={mb} />
          <InsulinResponse ir={ir} />
          <ActivityAssociations ap={ap} />
          <HealthEpisodes block={report.health_episodes} />
        </OptionalSection>
      )}

      <EvidenceContextBlock
        context={report.evidence_context}
        narrativeEvidenceIds={n?.evidence_item_ids}
        collapsible
      />

      {report.insurance?.available && <InsuranceBlock insurance={report.insurance} />}

      {/* Footer */}
      <div className="report-section text-[11px] text-muted-foreground border-t border-border pt-3 leading-relaxed">
        Generated by GlucoPilot on {new Date(report.generated_at).toLocaleDateString()} from CGM, pump, wearable and
        uploaded-record data. Glucose figures are calculated from CGM readings; GMI is an estimate, not a laboratory A1c.
        This summary supports the clinical conversation and is not a diagnosis or treatment recommendation.
      </div>
    </div>
  );
}
