import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, FileHeart, Loader2, Printer, RefreshCw } from "lucide-react";
import { useAuth } from "@/lib/AuthContext";
import { decideAuditReview, listAuditReviews, logAudit } from "@/lib/auditLog";

const MODES = [
  ["clinician", "Concise clinician"],
  ["endocrinology", "Endocrinology"],
  ["gastroenterology", "Gastroenterology"],
  ["neurology_autonomic", "Neurology / autonomic"],
  ["hematology", "Hematology"],
  ["gynecology_reproductive", "Gynecology / reproductive"],
  ["primary_care", "Primary care"],
];

// Long tables break between rows; headings stay with what follows them.
const PRINT_CSS = `
@media print {
  html { font-size: 12.5px; }
  html, body, main { background: #fff !important; }
  header, .print\\:hidden { display: none !important; }
  main { padding: 0 !important; max-width: 100% !important; }
  .report-section, tr { break-inside: avoid; }
  h1, h2, h3 { break-after: avoid; }
  body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  @page { margin: 1.2cm; }
}
`;

async function generate(mode, days) {
  const response = await fetch("/api/briefs/clinician", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode, days }),
  });
  if (!response.ok) throw new Error(`Brief generation failed (${response.status})`);
  return response.json();
}

const CONCERN_LABELS = {
  abnormal_lab: "Abnormal lab",
  abnormal_labs: "Abnormal labs",
  weight_loss: "Weight loss",
  hypoglycemia: "Hypoglycemia",
  reported_symptoms: "Reported symptoms",
};

const REPORTED_SYMPTOMS = /^\s*reported symptoms?:?\s*/i;
// Sentences that restate an item's strength or advise a conversation; the
// strength tag already says the first, and the brief does not advise.
const BOILERPLATE = /^an? (emerging|exploratory) (observational association|signal) was (detected|observed)( in this limited sample)?\.$|worth (discussing|reviewing)|(health ?care|care) team|suggest a need/i;

function readableType(entityType = "") {
  const words = String(entityType).replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function isAbnormal(flag) {
  return /high|low|abnormal|critical|out of range|positive/i.test(flag || "");
}

function plainLabel(label = "") {
  const text = String(label).replace(REPORTED_SYMPTOMS, "");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function factualDetail(text) {
  if (!text) return "";
  // Sentences end at punctuation followed by a space, so "90.7%" stays whole.
  return String(text).split(/(?<=[.!?])\s+/)
    .map((sentence) => sentence.trim())
    .filter((sentence) => sentence && !BOILERPLATE.test(sentence))
    .join(" ");
}

function Flag({ flag }) {
  if (!flag) return null;
  const tone = isAbnormal(flag) ? "border-red-300 bg-red-50 text-red-800" : "border-border bg-muted text-muted-foreground";
  return <span className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase ${tone}`}>{flag}</span>;
}

function SourceLinks({ links = [] }) {
  if (!links.length) return <span className="text-muted-foreground">No direct source link</span>;
  return links.map((link, index) => (
    <a key={`${link.href}-${index}`} href={link.href} className="text-primary underline mr-2">
      Open source evidence
    </a>
  ));
}

// Observed, confirmed and calculated items need no qualifier; weaker signals
// carry a one-word tag.
function StrengthTag({ strength }) {
  if (!strength || ["observed", "confirmed", "calculated"].includes(strength.status)) return null;
  return (
    <span className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-800">
      {String(strength.status).replaceAll("_", " ")} signal
    </span>
  );
}

function ReviewButton({ item, onReview }) {
  if (!onReview) return null;
  return (
    <button
      type="button"
      onClick={() => onReview({
        target_kind: "evidence_item",
        target_type: item.entity_type,
        target_id: item.id,
        target_label: item.display?.label || item.title || item.entity_type,
      })}
      className="print:hidden rounded-md border px-2 py-0.5 text-[11px]"
    >
      Annotate or review
    </button>
  );
}

// A symptom log's flag is its severity; a history entry's status is its type.
function rowDisplay(item) {
  const display = item.display || {};
  if (item.entity_type === "SymptomLog" && /^\d+$/.test(String(display.flag || ""))) {
    return { ...display, flag: null, status: `severity ${display.flag}` };
  }
  if (item.entity_type === "HistoryEntry" && display.status === "note") return { ...display, status: null };
  return display;
}

function ItemRow({ item, onReview }) {
  const display = rowDisplay(item);
  const reference = display.reference && (item.entity_type === "LabResult" ? `Reference ${display.reference}` : display.reference);
  // Analytics text is trimmed of boilerplate; recorded text is shown as written.
  const weak = item.evidence_strength && !["observed", "confirmed", "calculated"].includes(item.evidence_strength.status);
  const detail = item.concern?.reason || (weak ? factualDetail(display.detail) : display.detail);
  return (
    <div className="report-section border-t border-border first:border-t-0 py-2 text-xs">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="flex flex-wrap items-baseline gap-2">
          {item.concern && <span className="rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-red-800">{CONCERN_LABELS[item.concern.kind] || item.concern.kind}</span>}
          <b className="text-sm">{plainLabel(display.label || item.title || readableType(item.entity_type))}</b>
          {display.value && <span className="font-semibold">{display.value}{display.unit ? ` ${display.unit}` : ""}</span>}
          <Flag flag={display.flag} />
          {display.status && <span className="rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground">{display.status}</span>}
          <StrengthTag strength={item.evidence_strength} />
        </span>
        <span className="flex items-center gap-2">
          {display.date && <span className="text-muted-foreground tabular-nums">{display.date}</span>}
          <ReviewButton item={item} onReview={onReview} />
        </span>
      </div>
      {reference && <p className="mt-0.5 text-muted-foreground">{reference}</p>}
      {detail && <p className="mt-0.5 leading-relaxed">{detail}</p>}
      {item.data?.repeat_count > 1 && <p className="mt-0.5 text-muted-foreground">Detected in {item.data.repeat_count} analysis runs; the latest is shown.</p>}
      {item.source_links?.length > 0 && <p className="mt-0.5 print:hidden"><SourceLinks links={item.source_links} /></p>}
    </div>
  );
}

function ItemList({ items = [], empty = "None recorded in this window.", onReview = null }) {
  if (!items.length) return <p className="text-xs text-muted-foreground">{empty}</p>;
  return <div>{items.map((item) => <ItemRow key={item.id} item={item} onReview={onReview} />)}</div>;
}

// Symptom concerns appear once, as a list of their titles; the full text is
// in Symptoms & history.
function Concerns({ items = [], onReview }) {
  if (!items.length) {
    return <p className="text-xs text-muted-foreground">No concerns met the brief&apos;s rules (abnormal labs, weight loss, time below range, reported symptoms).</p>;
  }
  const symptoms = items.filter((item) => item.concern?.kind === "reported_symptoms");
  const others = items.filter((item) => item.concern?.kind !== "reported_symptoms");
  const dates = [...new Set(symptoms.map((item) => item.display?.date).filter(Boolean))];
  const symptomRow = symptoms.length > 0 && (
    <div key="reported-symptoms" className="report-section border-t border-border first:border-t-0 py-2 text-xs">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="flex flex-wrap items-baseline gap-2">
          <span className="rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-red-800">{CONCERN_LABELS.reported_symptoms}</span>
          <b className="text-sm">{symptoms.map((item) => plainLabel(item.display?.label || item.title)).join(" · ")}</b>
        </span>
        {dates.length > 0 && <span className="text-muted-foreground tabular-nums">{dates.join(", ")}</span>}
      </div>
    </div>
  );
  // Ranked order holds: symptoms take the place of the first symptom concern.
  const firstSymptom = items.findIndex((item) => item.concern?.kind === "reported_symptoms");
  const rows = others.map((item) => <ItemRow key={item.id} item={item} onReview={onReview} />);
  if (symptomRow) rows.splice(Math.min(firstSymptom, rows.length), 0, symptomRow);
  return <div>{rows}</div>;
}

function RecordList({ items = [], empty = "None recorded." }) {
  if (!items.length) return <p className="text-xs text-muted-foreground">{empty}</p>;
  return (
    <ul className="divide-y divide-border text-xs">
      {items.map((item) => {
        const display = item.display || {};
        const meta = [display.flag, display.status, display.date].filter(Boolean).join(" · ");
        return (
          <li key={item.id} className="flex flex-wrap justify-between gap-x-2 py-1">
            <span>
              <b>{display.label}</b>
              {display.value && <span> — {display.value}</span>}
              {display.detail && <span className="text-muted-foreground"> · {display.detail}</span>}
            </span>
            {meta && <span className="text-muted-foreground">{meta}</span>}
          </li>
        );
      })}
    </ul>
  );
}

function LabTable({ items = [], onReview = null }) {
  const [showAll, setShowAll] = useState(false);
  if (!items.length) return <p className="text-xs text-muted-foreground">No lab results recorded.</p>;
  const notable = items.filter((item) => isAbnormal(item.display?.flag) || item.display?.flag_note);
  const rows = showAll || !notable.length ? items : notable;
  const statuses = new Set(items.map((item) => item.display?.status || ""));
  // One status shared by every row is stated once above the table.
  const sharedStatus = statuses.size === 1 ? [...statuses][0] : "";
  return (
    <div className="space-y-2">
      {sharedStatus && (
        <p className="text-[11px] text-muted-foreground">Status of every result: {sharedStatus.toLowerCase()}.</p>
      )}
      {notable.length > 0 && notable.length < items.length && (
        <div className="print:hidden flex flex-wrap items-center gap-2 text-xs">
          <span className="text-muted-foreground">
            {showAll ? `All ${items.length} results` : `${notable.length} abnormal or flag-corrected of ${items.length} results`}
          </span>
          <button type="button" onClick={() => setShowAll((value) => !value)} className="rounded-md border px-2 py-0.5">
            {showAll ? "Show abnormal only" : `Show all ${items.length}`}
          </button>
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-left text-muted-foreground">
              <th className="py-1 pr-2 font-medium">Test</th>
              <th className="py-1 pr-2 font-medium">Result</th>
              <th className="py-1 pr-2 font-medium">Reference</th>
              <th className="py-1 pr-2 font-medium">Flag</th>
              <th className="py-1 pr-2 font-medium">Collected</th>
              <th className="py-1 font-medium print:hidden">Source</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => {
              const display = item.display || {};
              return (
                <tr key={item.id} className="border-b border-border align-top">
                  <td className="py-0.5 pr-2 font-medium">
                    {display.label}
                    {display.status && !sharedStatus && <span className="block text-[10px] font-normal text-amber-700">{display.status}</span>}
                  </td>
                  <td className={`py-0.5 pr-2 whitespace-nowrap ${isAbnormal(display.flag) ? "font-semibold text-red-800" : ""}`}>{display.value}{display.unit ? ` ${display.unit}` : ""}</td>
                  <td className="py-0.5 pr-2 text-muted-foreground whitespace-nowrap">{display.reference || "—"}</td>
                  <td className="py-0.5 pr-2">
                    <Flag flag={display.flag} />
                    {display.flag_note && <span className="block text-[10px] text-muted-foreground">{display.flag_note}</span>}
                  </td>
                  <td className="py-0.5 pr-2 whitespace-nowrap tabular-nums">{display.date || "—"}</td>
                  <td className="py-0.5 print:hidden whitespace-nowrap">
                    <SourceLinks links={item.source_links} />
                    <ReviewButton item={item} onReview={onReview} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// Documents print as a list of what is on file; the machine-written summary
// of each stays on screen behind a toggle.
function DocumentList({ items = [], onReview = null }) {
  if (!items.length) return <p className="text-xs text-muted-foreground">No clinical documents in this window.</p>;
  return (
    <ul className="divide-y divide-border text-xs">
      {items.map((item) => {
        const display = item.display || {};
        return (
          <li key={item.id} className="report-section py-1">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span><b>{display.label || item.title}</b>{display.value && <span className="text-muted-foreground"> · {display.value}</span>}</span>
              <span className="flex items-center gap-2">
                <span className="text-muted-foreground tabular-nums">{display.date || "undated"}</span>
                <ReviewButton item={item} onReview={onReview} />
              </span>
            </div>
            {display.detail && (
              <details className="print:hidden mt-0.5">
                <summary className="cursor-pointer text-[11px] text-muted-foreground">Machine-written summary</summary>
                <p className="mt-1 leading-relaxed">{display.detail}</p>
                <p className="mt-1"><SourceLinks links={item.source_links} /></p>
              </details>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function DisplayList({ entries = [], empty }) {
  if (!entries.length) return <p className="text-xs text-muted-foreground">{empty}</p>;
  return (
    <ul className="divide-y divide-border text-xs">
      {entries.map((entry, index) => {
        const display = entry.display || {};
        const label = display.label || entry.evidence?.display?.label || entry.code || "Entry";
        const detail = display.detail || entry.reason || entry.message;
        return (
          <li key={`${entry.id || entry.code || "entry"}-${index}`} className="py-1">
            <span className="flex flex-wrap items-center gap-2">
              <b>{label}</b>
              {display.value && <span>{display.value}{display.unit ? ` ${display.unit}` : ""}</span>}
              <Flag flag={display.flag} />
              {display.date && <span className="text-muted-foreground">{display.date}</span>}
            </span>
            {detail && <span className="block text-muted-foreground">{detail}</span>}
          </li>
        );
      })}
    </ul>
  );
}

function AppendixTable({ items = [] }) {
  if (!items.length) return <p className="text-xs text-muted-foreground">No evidence selected.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-border text-left text-muted-foreground">
            <th className="py-1 pr-2 font-medium">Item</th>
            <th className="py-1 pr-2 font-medium">Type</th>
            <th className="py-1 pr-2 font-medium">Date</th>
            <th className="py-1 pr-2 font-medium">Evidence</th>
            <th className="py-1 font-medium">Source</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id} className="border-b border-border align-top">
              <td className="py-0.5 pr-2">{item.display?.label || item.title}</td>
              <td className="py-0.5 pr-2 text-muted-foreground">{readableType(item.entity_type)}</td>
              <td className="py-0.5 pr-2 whitespace-nowrap">{item.display?.date || "—"}</td>
              <td className="py-0.5 pr-2 capitalize">{String(item.evidence_strength?.status || "").replaceAll("_", " ")}</td>
              <td className="py-0.5"><SourceLinks links={item.source_links} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Section({ title, children, className = "" }) {
  return (
    <section className={`rounded-lg border border-border bg-card p-3 space-y-1.5 ${className}`}>
      <h2 className="font-semibold text-sm">{title}</h2>
      {children}
    </section>
  );
}

// Reference material: collapsed on screen and never printed.
function ReferenceSection({ title, summary, children }) {
  const [open, setOpen] = useState(false);
  return (
    <section className="print:hidden rounded-lg border border-dashed border-border bg-muted/20">
      <h2 className="sr-only">{title}</h2>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="w-full flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-left"
      >
        <span className="inline-flex items-center gap-2 text-sm font-medium">
          {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          {title}
        </span>
        {summary && <span className="text-[11px] text-muted-foreground">{summary}</span>}
      </button>
      <div className={`${open ? "" : "hidden"} px-3 pb-3 space-y-2`}>{children}</div>
    </section>
  );
}

export default function ClinicianBrief() {
  const { isProvider, isAdmin } = useAuth();
  const [mode, setMode] = useState("clinician");
  const [days, setDays] = useState(90);
  const [brief, setBrief] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reviews, setReviews] = useState([]);
  const [reviewTarget, setReviewTarget] = useState(null);
  const [reviewAction, setReviewAction] = useState("annotate");
  const [reviewText, setReviewText] = useState("");
  const [reviewMessage, setReviewMessage] = useState("");
  const [ownerReason, setOwnerReason] = useState("");

  const refreshReviews = useCallback(async () => {
    const result = await listAuditReviews();
    setReviews(result.reviews || []);
  }, []);

  const chooseReviewTarget = useCallback((target) => {
    setReviewTarget(target);
    setReviewAction("annotate");
    setReviewText("");
    setReviewMessage("");
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setBrief(await generate(mode, days));
    } catch (failure) {
      setError(failure.message);
    } finally {
      setLoading(false);
    }
  }, [mode, days]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    void refreshReviews().catch(() => setReviewMessage("Clinical review history is unavailable."));
  }, [refreshReviews]);

  const submitReview = useCallback(async () => {
    if (!reviewTarget) return;
    setReviewMessage("");
    try {
      await logAudit({
        ...reviewTarget,
        action: reviewAction,
        text: reviewText,
        evidence_bundle_id: brief?.evidence_bundle?.id,
      });
      setReviewText("");
      setReviewTarget(null);
      await refreshReviews();
      setReviewMessage("Provider review recorded with immutable attribution.");
    } catch (failure) {
      setReviewMessage(failure.message);
    }
  }, [brief?.evidence_bundle?.id, refreshReviews, reviewAction, reviewTarget, reviewText]);

  const ownerDecision = useCallback(async (reviewId, decision) => {
    if (!ownerReason.trim()) {
      setReviewMessage("Enter a reason before accepting or disputing a review.");
      return;
    }
    try {
      await decideAuditReview(reviewId, decision, ownerReason);
      setOwnerReason("");
      await refreshReviews();
      setReviewMessage(`Review ${decision === "accept" ? "accepted" : "disputed"}; clinician history retained.`);
    } catch (failure) {
      setReviewMessage(failure.message);
    }
  }, [ownerReason, refreshReviews]);

  if (loading && !brief) {
    return <div className="h-64 flex items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-primary" /></div>;
  }

  const sections = brief?.sections || {};
  const reviewer = isProvider ? chooseReviewTarget : null;
  const concerns = sections.concerns || [];
  const weightIsConcern = concerns.some((item) => item.concern?.kind === "weight_loss");
  const windowText = brief?.window?.start && brief?.window?.end
    ? `${brief.window.start.slice(0, 10)} to ${brief.window.end.slice(0, 10)}`
    : null;
  const reassuring = sections.reassuring_evidence || [];
  const opposing = sections.opposing_evidence || [];
  return (
    <div className="space-y-3 max-w-5xl mx-auto">
      <style>{PRINT_CSS}</style>
      <div className="print:hidden flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold flex items-center gap-2"><FileHeart className="h-5 w-5 text-primary" /> Clinician brief</h1>
          <p className="text-xs text-muted-foreground">Evidence filtered for one specialty, with links to each source.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <select aria-label="Brief specialty" value={mode} onChange={(event) => setMode(event.target.value)} className="h-9 rounded-md border bg-background px-2 text-sm">
            {MODES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <select aria-label="Brief date range" value={days} onChange={(event) => setDays(Number(event.target.value))} className="h-9 rounded-md border bg-background px-2 text-sm">
            {[30, 90, 180, 365].map((value) => <option key={value} value={value}>{value} days</option>)}
          </select>
          <button onClick={load} className="h-9 px-3 rounded-md border text-sm flex items-center gap-1"><RefreshCw className="h-4 w-4" /> Refresh</button>
          <button onClick={() => window.print()} className="h-9 px-3 rounded-md border text-sm flex items-center gap-1"><Printer className="h-4 w-4" /> Print</button>
        </div>
      </div>

      {error && <p className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800">{error}</p>}
      {reviewMessage && <p className="rounded-lg border border-border p-3 text-sm">{reviewMessage}</p>}
      {isProvider && reviewTarget && (
        <div className="print:hidden rounded-xl border border-sky-200 bg-sky-50 p-4 space-y-3">
          <h2 className="font-semibold">Provider review: {reviewTarget.target_label}</h2>
          <select aria-label="Review action" value={reviewAction} onChange={(event) => setReviewAction(event.target.value)} className="h-9 rounded-md border bg-background px-2 text-sm">
            <option value="annotate">Annotation</option>
            <option value="mark_reviewed">Mark reviewed</option>
            <option value="question">Question</option>
            {reviewTarget.target_kind === "hypothesis" && <option value="hypothesis_confirm">Confirm hypothesis</option>}
            {reviewTarget.target_kind === "hypothesis" && <option value="hypothesis_reject">Reject hypothesis</option>}
          </select>
          <textarea aria-label="Review text" value={reviewText} onChange={(event) => setReviewText(event.target.value)} className="w-full rounded-md border bg-background p-2 text-sm" rows={3} placeholder="Attributable clinical note, rationale, or question" />
          <div className="flex gap-2">
            <button type="button" onClick={submitReview} className="rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground">Record review</button>
            <button type="button" onClick={() => setReviewTarget(null)} className="rounded-md border px-3 py-2 text-sm">Cancel</button>
          </div>
          <p className="text-xs text-muted-foreground">This appends an audit event. It does not change source or canonical observations.</p>
        </div>
      )}
      {brief && (
        <>
          <div className="border-b border-border pb-2">
            <h2 className="text-xl font-bold">{brief.mode_label} brief</h2>
            <p className="text-xs text-muted-foreground">
              {brief.window.days}-day window{windowText ? ` · ${windowText}` : ""}
              {brief.generated_at ? ` · generated ${brief.generated_at.slice(0, 10)}` : ""}
            </p>
          </div>

          <Section title="Concerns">
            <Concerns items={concerns} onReview={reviewer} />
          </Section>

          <div className={`grid gap-3 md:grid-cols-3 print:grid-cols-3 ${weightIsConcern ? "" : "lg:grid-cols-4 print:grid-cols-4"}`}>
            <Section title="Active problems"><RecordList items={sections.problems} empty="No diagnoses recorded." /></Section>
            <Section title="Medications & supplements"><RecordList items={sections.medications} empty="No active medications recorded." /></Section>
            <Section title="Allergies"><RecordList items={sections.allergies} empty="No allergies recorded." /></Section>
            {!weightIsConcern && <Section title="Weight"><ItemList items={sections.weight} empty="No weight entries recorded." /></Section>}
          </div>
          <Section title="Glucose & insulin"><ItemList items={sections.glucose_insulin} empty="No CGM or pump data in this window." onReview={reviewer} /></Section>
          <Section title="Labs — most recent value of each test"><LabTable items={sections.labs} onReview={reviewer} /></Section>
          <Section title="Symptoms & history"><ItemList items={sections.symptoms_history} onReview={reviewer} /></Section>
          <Section title="Imaging & clinical documents"><DocumentList items={sections.documents} onReview={reviewer} /></Section>
          <Section title="Objective patterns">
            <ItemList
              items={(sections.objective_patterns || []).filter((item) => item.display?.value || item.display?.detail)}
              empty="No active patterns or insights."
              onReview={reviewer}
            />
          </Section>
          <div className="grid gap-3 md:grid-cols-2 print:grid-cols-2">
            {(sections.wearables_cycle || []).length > 0 && (
              <Section title="Wearables & cycle"><ItemList items={sections.wearables_cycle} onReview={reviewer} /></Section>
            )}
            <Section title="Management burden"><ItemList items={sections.management_burden} onReview={reviewer} /></Section>
          </div>
          <Section title="Hypotheses — not diagnoses">
            {!sections.hypotheses?.length ? <p className="text-xs text-muted-foreground">No hypotheses listed for this brief.</p> : sections.hypotheses.map((item) => (
              <div key={item.id} className="report-section border-t border-border first:border-t-0 py-1.5 text-xs">
                <p className="flex flex-wrap items-center gap-2">
                  {!item.definitive_allowed && <span className="rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-amber-800">{item.status_label || "Unconfirmed"}</span>}
                  <span className={item.evidence_state === "not_evaluated" ? "text-muted-foreground" : "font-semibold"}>{item.title}</span>
                  {isProvider && (
                    <button type="button" onClick={() => chooseReviewTarget({
                      target_kind: "hypothesis",
                      target_type: "HealthHypothesis",
                      target_id: item.id,
                      target_label: item.title,
                    })} className="print:hidden rounded-md border px-2 py-0.5 text-[11px]">
                      Review hypothesis
                    </button>
                  )}
                </p>
                <p className={item.evidence_state === "not_evaluated" ? "text-muted-foreground" : item.definitive_allowed ? "" : "text-amber-700"}>{item.display_label}</p>
                {item.description && <p>{item.description}</p>}
              </div>
            ))}
          </Section>
          <Section title="Questions for the visit">
            <ul className="list-disc pl-5 text-sm space-y-0.5">{(sections.questions || []).map((question) => <li key={question}>{question}</li>)}</ul>
          </Section>
          <Section title="Contradictions & limitations">
            {(sections.contradictions || []).map((item) => <p key={item.id} className="text-xs"><b className="capitalize">{item.severity}</b> — {item.explanation}</p>)}
            <DisplayList entries={sections.limitations} empty="No limitations recorded." />
          </Section>

          <ReferenceSection
            title="Reassuring & opposing evidence"
            summary={`${reassuring.length} reassuring · ${opposing.length} opposing · not printed`}
          >
            <p className="text-xs font-medium">Reassuring</p>
            <DisplayList entries={reassuring} empty="No selected reassuring evidence." />
            <p className="text-xs font-medium pt-2">Opposing / contradictory</p>
            <DisplayList entries={opposing} empty="No selected opposing evidence." />
          </ReferenceSection>
          <ReferenceSection title="Evidence appendix" summary={`${(brief.appendix || []).length} items · not printed`}>
            <AppendixTable items={brief.appendix} />
          </ReferenceSection>
          <section className={`rounded-lg border border-border bg-card p-3 space-y-2 ${reviews.length ? "" : "print:hidden"}`}>
            <h2 className="font-semibold text-sm">Clinical review history</h2>
            {!reviews.length && <p className="text-xs text-muted-foreground">No provider reviews recorded.</p>}
            {isAdmin && reviews.length > 0 && (
              <textarea aria-label="Owner review reason" value={ownerReason} onChange={(event) => setOwnerReason(event.target.value)} className="print:hidden w-full rounded-md border p-2 text-sm" rows={2} placeholder="Reason for accepting or disputing a provider review" />
            )}
            {reviews.map((review) => (
              <div key={review.id} className="rounded-lg border p-3 text-xs">
                <div className="flex flex-wrap justify-between gap-2"><b>{review.target_label || review.target_type}</b><span>{review.provider_status} · owner {review.owner_status}</span></div>
                <p className="mt-1">{review.current_text || "No additional note."}</p>
                <p className="text-muted-foreground">{review.events?.length || 0} immutable audit event(s)</p>
                {isAdmin && (
                  <div className="print:hidden mt-2 flex gap-2">
                    <button type="button" onClick={() => ownerDecision(review.id, "accept")} className="rounded-md border px-2 py-1">Accept</button>
                    <button type="button" onClick={() => ownerDecision(review.id, "dispute")} className="rounded-md border px-2 py-1">Dispute</button>
                  </div>
                )}
              </div>
            ))}
          </section>

          <div className="border-t border-border pt-2 text-[11px] text-muted-foreground space-y-0.5">
            <p>{brief.privacy?.note}</p>
            <p>{[brief.language?.hypotheses, brief.language?.associations, brief.language?.clinical].filter(Boolean).join(" ")}</p>
            <p>Evidence Bundle {brief.evidence_bundle?.version}.</p>
          </div>
        </>
      )}
    </div>
  );
}
