import { useCallback, useEffect, useState } from "react";
import { FileHeart, Loader2, Printer, RefreshCw } from "lucide-react";
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

function SourceLinks({ links = [] }) {
  if (!links.length) return <span className="text-muted-foreground">No direct source link</span>;
  return links.map((link, index) => (
    <a key={`${link.href}-${index}`} href={link.href} className="text-primary underline mr-2">
      Open source evidence
    </a>
  ));
}

function EvidenceList({ items = [], empty = "No specialty-relevant evidence selected.", onReview = null }) {
  if (!items.length) return <p className="text-xs text-muted-foreground">{empty}</p>;
  return (
    <div className="space-y-2">
      {items.map((item) => (
        <div key={item.id} className="rounded-lg border border-border p-3 text-xs">
          <div className="flex flex-wrap justify-between gap-2">
            <b>{item.title || item.entity_type}</b>
            <span className="text-muted-foreground">{item.entity_type}</span>
          </div>
          {item.evidence_strength && (
            <p className="mt-1">
              <span className="font-medium capitalize">{String(item.evidence_strength.status).replaceAll("_", " ")}</span>
              {" — "}{item.evidence_strength.lead}
            </p>
          )}
          <p className="mt-1"><SourceLinks links={item.source_links} /></p>
          {onReview && (
            <button
              type="button"
              onClick={() => onReview({
                target_kind: "evidence_item",
                target_type: item.entity_type,
                target_id: item.id,
                target_label: item.title || item.entity_type,
              })}
              className="mt-2 rounded-md border px-2 py-1 text-xs"
            >
              Annotate or review
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

function Section({ title, children }) {
  return (
    <section className="report-section rounded-xl border border-border bg-card p-4 space-y-2">
      <h2 className="font-semibold">{title}</h2>
      {children}
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
  return (
    <div className="space-y-4 max-w-5xl mx-auto">
      <div className="print:hidden flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold flex items-center gap-2"><FileHeart className="h-5 w-5 text-primary" /> Evidence-linked clinician brief</h1>
          <p className="text-xs text-muted-foreground">Specialty-minimized evidence with source drill-down and strength-aware language.</p>
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
          <div className="rounded-xl border border-border p-4">
            <h2 className="font-semibold">{brief.mode_label} brief</h2>
            <p className="text-xs text-muted-foreground">{brief.window.days}-day evidence window · Evidence Bundle {brief.evidence_bundle.version}</p>
            <p className="text-xs mt-2">{brief.privacy.note}</p>
            <p className="text-xs text-muted-foreground">{brief.language.hypotheses} {brief.language.associations}</p>
          </div>

          <Section title="Concerns"><EvidenceList items={sections.concerns} onReview={isProvider ? chooseReviewTarget : null} /></Section>
          <Section title="Objective patterns"><EvidenceList items={sections.objective_patterns} onReview={isProvider ? chooseReviewTarget : null} /></Section>
          <Section title="Glucose & insulin"><EvidenceList items={sections.glucose_insulin} onReview={isProvider ? chooseReviewTarget : null} /></Section>
          <Section title="Management burden"><EvidenceList items={sections.management_burden} onReview={isProvider ? chooseReviewTarget : null} /></Section>
          <Section title="Labs & imaging"><EvidenceList items={sections.labs_imaging} onReview={isProvider ? chooseReviewTarget : null} /></Section>
          <Section title="Hypotheses — not diagnoses">
            {!sections.hypotheses?.length ? <p className="text-xs text-muted-foreground">No specialty-relevant hypotheses selected.</p> : sections.hypotheses.map((item) => (
              <div key={item.id} className="rounded-lg border p-3 text-xs">
                <b>{item.title}</b>
                <p className={item.definitive_allowed ? "" : "text-amber-700"}>{item.display_label}</p>
                <p>{item.description}</p>
                {isProvider && (
                  <button type="button" onClick={() => chooseReviewTarget({
                    target_kind: "hypothesis",
                    target_type: "HealthHypothesis",
                    target_id: item.id,
                    target_label: item.title,
                  })} className="mt-2 rounded-md border px-2 py-1 text-xs">
                    Review hypothesis
                  </button>
                )}
              </div>
            ))}
          </Section>
          <Section title="Reassuring & opposing evidence">
            <p className="text-xs font-medium">Reassuring</p>
            <EvidenceList items={(sections.reassuring_evidence || []).map((entry) => entry.evidence).filter(Boolean)} />
            <p className="text-xs font-medium pt-2">Opposing / contradictory</p>
            {(sections.opposing_evidence || []).length === 0 ? <p className="text-xs text-muted-foreground">No selected opposing evidence.</p> : (
              <p className="text-xs">{sections.opposing_evidence.length} opposing or contradiction entries retained.</p>
            )}
          </Section>
          <Section title="Contradictions & limitations">
            {(sections.contradictions || []).map((item) => <p key={item.id} className="text-xs"><b className="capitalize">{item.severity}</b> — {item.explanation}</p>)}
            {(sections.limitations || []).map((item, index) => <p key={`${item.code}-${index}`} className="text-xs text-muted-foreground">{item.message}</p>)}
          </Section>
          <Section title="Questions for the visit">
            <ul className="list-disc pl-5 text-sm space-y-1">{sections.questions.map((question) => <li key={question}>{question}</li>)}</ul>
          </Section>
          <Section title="Evidence appendix">
            <EvidenceList items={brief.appendix} onReview={isProvider ? chooseReviewTarget : null} />
          </Section>
          <Section title="Clinical review history">
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
          </Section>
        </>
      )}
    </div>
  );
}
