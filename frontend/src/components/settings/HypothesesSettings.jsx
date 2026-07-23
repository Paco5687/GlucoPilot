import { useEffect, useState } from "react";
import { AlertTriangle, Beaker, CheckCircle2, Loader2, Plus, ShieldAlert, XCircle } from "lucide-react";
import { toast } from "sonner";

/** @param {import("react").InputHTMLAttributes<HTMLInputElement>} props */
function Input({ className = "", ...props }) {
  return <input className={`h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm ${className}`} {...props} />;
}

/** @param {import("react").ButtonHTMLAttributes<HTMLButtonElement> & {variant?: string, size?: string}} props */
function Button({ className = "", variant = "default", size = "default", ...props }) {
  const tone = variant === "outline"
    ? "border border-input bg-transparent hover:bg-accent"
    : variant === "ghost"
    ? "hover:bg-accent"
    : "bg-primary text-primary-foreground hover:bg-primary/90";
  const dimensions = size === "sm" ? "h-8 px-3 text-xs" : "h-9 px-4 text-sm";
  return (
    <button
      className={`inline-flex items-center justify-center rounded-md font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 ${tone} ${dimensions} ${className}`}
      {...props}
    />
  );
}

const STATUS = {
  proposed: ["Proposed", "bg-amber-100 text-amber-800 border-amber-200"],
  under_review: ["Under review", "bg-blue-100 text-blue-800 border-blue-200"],
  confirmed: ["Clinician confirmed", "bg-emerald-100 text-emerald-800 border-emerald-200"],
  ruled_against: ["Clinician ruled against", "bg-slate-100 text-slate-700 border-slate-200"],
  archived: ["Archived", "bg-muted text-muted-foreground border-border"],
};

const ROLE = {
  supporting: ["Supporting", "border-emerald-200 bg-emerald-50 text-emerald-900"],
  opposing: ["Opposing", "border-rose-200 bg-rose-50 text-rose-900"],
  missing: ["Missing / needed", "border-amber-200 bg-amber-50 text-amber-900"],
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    ...options,
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.detail || `Request failed (${response.status})`);
  return data;
}

function EvidenceColumns({ hypothesis }) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-2">
      {Object.entries(ROLE).map(([role, [label, tone]]) => {
        const items = hypothesis.evidence_by_role?.[role] || [];
        return (
          <div key={role} className={`rounded-lg border p-2.5 ${tone}`}>
            <p className="text-[11px] font-semibold uppercase tracking-wide">{label}</p>
            {items.length ? (
              <ul className="mt-1.5 space-y-1.5 text-xs">
                {items.map((item, index) => (
                  <li key={`${item.source_id || "missing"}-${index}`}>
                    <span>{item.summary}</span>
                    {item.source_link?.href && (
                      <a
                        href={item.source_link.href}
                        target="_blank"
                        rel="noreferrer"
                        className="ml-1 underline font-medium"
                      >
                        source
                      </a>
                    )}
                    <span className="block opacity-70">
                      weight {Number(item.weight).toFixed(2)}
                      {item.source_version ? ` · version ${item.source_version}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1.5 text-xs opacity-70">None recorded.</p>
            )}
          </div>
        );
      })}
    </div>
  );
}

function AddEvidence({ hypothesis, onSaved }) {
  const [open, setOpen] = useState(false);
  const [role, setRole] = useState("supporting");
  const [sourceId, setSourceId] = useState("");
  const [sourceVersion, setSourceVersion] = useState("");
  const [summary, setSummary] = useState("");
  const [weight, setWeight] = useState("1");
  const [busy, setBusy] = useState(false);

  async function save() {
    if (!summary.trim() || (role !== "missing" && !sourceId.trim())) return;
    const item = role === "missing"
      ? {
          role,
          source_kind: "missing",
          source_id: null,
          source_type: "",
          source_version: "",
          summary: summary.trim(),
          weight: Number(weight),
          source_link: {},
        }
      : {
          role,
          source_kind: "patient_report",
          source_id: sourceId.trim(),
          source_type: "manual_reference",
          source_version: sourceVersion.trim(),
          summary: summary.trim(),
          weight: Number(weight),
          source_link: {},
        };
    setBusy(true);
    try {
      const updated = await api(`/api/hypotheses/${hypothesis.id}/evidence`, {
        method: "PUT",
        body: JSON.stringify({
          reason: `Added ${role} evidence through Settings.`,
          evidence: [...(hypothesis.evidence || []), item],
        }),
      });
      onSaved(updated);
      setSummary("");
      setSourceId("");
      setSourceVersion("");
      setWeight("1");
      setOpen(false);
    } catch (error) {
      toast.error(error.message);
    }
    setBusy(false);
  }

  if (!open) {
    return (
      <Button size="sm" variant="outline" onClick={() => setOpen(true)} className="gap-1.5">
        <Plus className="w-3.5 h-3.5" /> Add evidence
      </Button>
    );
  }
  return (
    <div className="rounded-lg border border-border bg-muted/20 p-3 space-y-2">
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-2">
        <select
          aria-label="Evidence role"
          value={role}
          onChange={(event) => setRole(event.target.value)}
          className="h-9 rounded-md border border-border bg-background px-2 text-sm"
        >
          <option value="supporting">supporting</option>
          <option value="opposing">opposing</option>
          <option value="missing">missing / needed</option>
        </select>
        {role !== "missing" && (
          <>
            <Input
              aria-label="Evidence source identity"
              value={sourceId}
              onChange={(event) => setSourceId(event.target.value)}
              placeholder="Source identity"
              className="text-sm"
            />
            <Input
              aria-label="Evidence source version"
              value={sourceVersion}
              onChange={(event) => setSourceVersion(event.target.value)}
              placeholder="Source version"
              className="text-sm"
            />
          </>
        )}
        <Input
          aria-label="Evidence weight"
          type="number"
          min="0.01"
          max="1"
          step="0.05"
          value={weight}
          onChange={(event) => setWeight(event.target.value)}
          className="text-sm"
        />
      </div>
      <Input
        aria-label="Evidence summary"
        value={summary}
        onChange={(event) => setSummary(event.target.value)}
        placeholder={role === "missing" ? "What evidence or test is still needed?" : "What does this source show?"}
        className="text-sm"
      />
      <div className="flex gap-2">
        <Button
          size="sm"
          onClick={save}
          disabled={busy || !summary.trim() || (role !== "missing" && !sourceId.trim())}
        >
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : "Save evidence revision"}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
      </div>
    </div>
  );
}

export default function HypothesesSettings() {
  const [items, setItems] = useState([]);
  const [canEdit, setCanEdit] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [origin, setOrigin] = useState("patient");
  const [originLabel, setOriginLabel] = useState("");
  const [verification, setVerification] = useState("");
  const [reviewAt, setReviewAt] = useState("");

  async function load() {
    try {
      const data = await api("/api/hypotheses");
      setItems(data.hypotheses || []);
      setCanEdit(Boolean(data.can_edit));
    } catch (error) {
      toast.error(error.message);
    }
    setLoading(false);
  }

  useEffect(() => { load(); }, []);

  function replace(updated) {
    setItems((current) => current.map((item) => item.id === updated.id ? updated : item));
  }

  async function create() {
    if (!title.trim()) return;
    setSaving(true);
    try {
      const created = await api("/api/hypotheses", {
        method: "POST",
        body: JSON.stringify({
          title: title.trim(),
          description: description.trim(),
          origin_kind: origin,
          origin_label: originLabel.trim(),
          suggested_verification: verification.trim(),
          review_at: reviewAt || null,
        }),
      });
      setItems((current) => [created, ...current]);
      setTitle("");
      setDescription("");
      setOrigin("patient");
      setOriginLabel("");
      setVerification("");
      setReviewAt("");
    } catch (error) {
      toast.error(error.message);
    }
    setSaving(false);
  }

  async function transition(item, status) {
    let reviewer = null;
    let decisionAuthority = null;
    const reason = window.prompt(
      status === "under_review"
        ? "Why is this ready for review?"
        : status === "archived"
        ? "Why is this being archived?"
        : `Clinical rationale for marking this ${status.replace("_", " ")}:`,
    );
    if (!reason?.trim()) return;
    if (status === "confirmed" || status === "ruled_against") {
      reviewer = window.prompt("Clinician name or accountable clinical reviewer:");
      if (!reviewer?.trim()) return;
      decisionAuthority = "clinician";
    }
    try {
      replace(await api(`/api/hypotheses/${item.id}/transition`, {
        method: "POST",
        body: JSON.stringify({
          status,
          reason: reason.trim(),
          reviewer: reviewer?.trim() || null,
          decision_authority: decisionAuthority,
        }),
      }));
    } catch (error) {
      toast.error(error.message);
    }
  }

  if (loading) return null;

  return (
    <div className="bg-card rounded-xl border border-amber-200 p-5 space-y-4">
      <div className="flex items-start gap-2">
        <Beaker className="w-5 h-5 text-amber-700 mt-0.5" />
        <div>
          <h3 className="font-semibold text-sm">Health hypotheses</h3>
          <p className="text-xs text-muted-foreground">
            Tentative ideas to investigate—not diagnoses. Confidence shows the recorded evidence balance,
            not the probability that a condition is present.
          </p>
        </div>
      </div>

      {items.length > 0 && (
        <div className="space-y-3">
          {items.map((item) => {
            const [statusLabel, statusTone] = STATUS[item.status] || [item.status, "bg-muted"];
            return (
              <article key={item.id} className="rounded-xl border border-amber-200 bg-amber-50/30 p-4 space-y-3">
                <div className="flex flex-wrap items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="text-[10px] font-bold uppercase tracking-wider text-amber-800">
                      Hypothesis · not a diagnosis
                    </p>
                    <h4 className="font-semibold text-sm">{item.title}</h4>
                    {item.description && <p className="text-xs text-muted-foreground mt-1">{item.description}</p>}
                  </div>
                  <span className={`text-[10px] px-2 py-1 rounded-full border font-semibold ${statusTone}`}>
                    {statusLabel}
                  </span>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs">
                  <div><span className="text-muted-foreground">Origin</span><br />{item.origin_kind} · {item.origin_label}</div>
                  <div>
                    <span className="text-muted-foreground">Evidence balance</span><br />
                    {Math.round(Number(item.confidence_score) * 100)}% · {item.confidence_label}
                  </div>
                  <div>
                    <span className="text-muted-foreground">Evidence version</span><br />
                    revision {item.evidence_revision}
                  </div>
                </div>
                <p className="text-[11px] text-muted-foreground">{item.confidence_rationale}</p>
                <EvidenceColumns hypothesis={item} />
                {(item.suggested_verification || item.review_at) && (
                  <div className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-xs text-blue-900">
                    <span className="font-semibold">Suggested verification:</span>{" "}
                    {item.suggested_verification || "No verification step recorded."}
                    {item.review_at ? ` · review ${item.review_at}` : ""}
                  </div>
                )}
                {item.decided_by && (
                  <p className="text-xs font-medium">
                    Decision recorded by {item.decided_by} at {item.decided_at}
                  </p>
                )}
                {canEdit && !["confirmed", "ruled_against", "archived"].includes(item.status) && (
                  <div className="space-y-2">
                    <AddEvidence hypothesis={item} onSaved={replace} />
                    <div className="flex flex-wrap gap-2">
                      {item.status === "proposed" && (
                        <Button size="sm" variant="outline" onClick={() => transition(item, "under_review")} className="gap-1.5">
                          <ShieldAlert className="w-3.5 h-3.5" /> Begin review
                        </Button>
                      )}
                      {item.status === "under_review" && (
                        <>
                          <Button size="sm" variant="outline" onClick={() => transition(item, "confirmed")} className="gap-1.5 text-emerald-700">
                            <CheckCircle2 className="w-3.5 h-3.5" /> Record clinician confirmation
                          </Button>
                          <Button size="sm" variant="outline" onClick={() => transition(item, "ruled_against")} className="gap-1.5">
                            <XCircle className="w-3.5 h-3.5" /> Record clinician ruling
                          </Button>
                        </>
                      )}
                      <Button size="sm" variant="ghost" onClick={() => transition(item, "archived")}>
                        Archive
                      </Button>
                    </div>
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}

      {canEdit && (
        <div className="rounded-xl border border-border bg-muted/20 p-3 space-y-2">
          <div className="flex items-center gap-1.5 text-xs font-semibold">
            <AlertTriangle className="w-3.5 h-3.5 text-amber-700" /> Record a tentative hypothesis
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <Input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Hypothesis to investigate" />
            <Input value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Why it is being considered" />
            <select
              aria-label="Hypothesis origin"
              value={origin}
              onChange={(event) => setOrigin(event.target.value)}
              className="h-9 rounded-md border border-border bg-background px-2 text-sm"
            >
              <option value="patient">patient-originated</option>
              <option value="clinician">clinician-originated</option>
              <option value="algorithm">algorithm-originated</option>
            </select>
            <Input value={originLabel} onChange={(event) => setOriginLabel(event.target.value)} placeholder="Origin name or algorithm ID" />
            <Input value={verification} onChange={(event) => setVerification(event.target.value)} placeholder="Suggested test or verification" />
            <Input aria-label="Hypothesis review date" type="date" value={reviewAt} onChange={(event) => setReviewAt(event.target.value)} />
          </div>
          <Button size="sm" onClick={create} disabled={saving || !title.trim()} className="gap-1.5">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} Add hypothesis
          </Button>
        </div>
      )}
    </div>
  );
}
