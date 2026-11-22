import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Beaker, FlaskConical, Loader2, Plus } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import LabLookupNote from "@/components/hypotheses/LabLookupNote";
import LabSuggestions from "@/components/hypotheses/LabSuggestions";
import useLabLookups from "@/components/hypotheses/useLabLookups";

// Dashboard view of the hypothesis ledger: things that may be wrong and still
// need a diagnosis. It reads and writes the same /api/hypotheses ledger as
// Settings. A new hypothesis opens its lab suggestions straight away; the
// server's queue looks up the rest, and each shows how many stored labs are
// still to review. Picked labs are saved as evidence. Clinician decisions stay in Settings, where the
// full review controls live.

const OPEN = new Set(["proposed", "under_review"]);

const STATUS = {
  proposed: ["Needs workup", "bg-amber-100 text-amber-800 border-amber-200"],
  under_review: ["Under review", "bg-blue-100 text-blue-800 border-blue-200"],
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

function EvidenceLine({ item }) {
  const supporting = item.evidence_by_role?.supporting?.length || 0;
  const opposing = item.evidence_by_role?.opposing?.length || 0;
  const missing = item.evidence_by_role?.missing?.length || 0;
  if (!supporting && !opposing && !missing) {
    return <p className="text-[11px] text-muted-foreground">No evidence recorded yet.</p>;
  }
  return (
    <p className="text-[11px] text-muted-foreground">
      {supporting} supporting · {opposing} against · {missing} still needed
    </p>
  );
}

export default function HypothesesCard() {
  const [items, setItems] = useState([]);
  const [canEdit, setCanEdit] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState("");
  const [why, setWhy] = useState("");
  const [confirmBy, setConfirmBy] = useState("");
  const [saving, setSaving] = useState(false);
  const [labsFor, setLabsFor] = useState(null);
  const lookups = useLabLookups(canEdit);

  useEffect(() => {
    let active = true;
    api("/api/hypotheses")
      .then((data) => {
        if (!active) return;
        setItems(data?.hypotheses || []);
        setCanEdit(Boolean(data?.can_edit));
      })
      .catch(() => { if (active) setLoadError(true); })
      .finally(() => { if (active) setLoaded(true); });
    return () => { active = false; };
  }, []);

  const open = items.filter((item) => OPEN.has(item.status));
  const decided = items.filter((item) => item.status === "confirmed" || item.status === "ruled_against");
  const showForm = canEdit && (adding || open.length === 0);

  async function save() {
    if (!title.trim()) return;
    setSaving(true);
    try {
      const created = await api("/api/hypotheses", {
        method: "POST",
        body: JSON.stringify({
          title: title.trim(),
          description: why.trim(),
          origin_kind: "patient",
          suggested_verification: confirmBy.trim(),
        }),
      });
      setItems((current) => [created, ...current]);
      setLabsFor(created.id);
      setTitle("");
      setWhy("");
      setConfirmBy("");
      setAdding(false);
      toast.success("Hypothesis added");
    } catch (error) {
      toast.error(error.message);
    }
    setSaving(false);
  }

  function replace(updated) {
    setItems((current) => current.map((item) => (item.id === updated.id ? updated : item)));
  }

  function closeLabs() {
    setLabsFor(null);
    lookups.refresh();
  }

  if (!loaded) return null;
  // Read-only viewers have nothing to add, so an empty card is noise.
  if (!canEdit && !loadError && open.length === 0) return null;

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-start gap-2 mb-3">
        <Beaker className="w-4 h-4 text-amber-700 mt-0.5" />
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold">Hypotheses</h3>
          <p className="text-[11px] text-muted-foreground">
            Possible problems that still need a diagnosis. Not diagnoses.
          </p>
        </div>
        {canEdit && !showForm && (
          <Button size="sm" variant="outline" onClick={() => setAdding(true)} className="gap-1.5">
            <Plus className="w-3.5 h-3.5" /> Add
          </Button>
        )}
      </div>

      {loadError && <p className="text-xs text-muted-foreground">Hypotheses could not be loaded.</p>}

      {open.length > 0 && (
        <ul className="space-y-2">
          {open.map((item) => {
            const [label, tone] = STATUS[item.status];
            return (
              <li key={item.id} className="rounded-lg border border-amber-200 bg-amber-50/30 px-3 py-2 space-y-1">
                <div className="flex items-start gap-2">
                  <p className="flex-1 min-w-0 text-sm font-medium">{item.title}</p>
                  <span className={`text-[10px] px-2 py-0.5 rounded-full border font-semibold whitespace-nowrap ${tone}`}>
                    {label}
                  </span>
                </div>
                {item.description && <p className="text-xs text-muted-foreground">{item.description}</p>}
                {item.suggested_verification && (
                  <p className="text-xs">
                    <span className="text-muted-foreground">To confirm or rule out:</span> {item.suggested_verification}
                  </p>
                )}
                <div className="flex flex-wrap items-center gap-3">
                  <EvidenceLine item={item} />
                  {canEdit && labsFor !== item.id && (
                    <button
                      type="button"
                      onClick={() => setLabsFor(item.id)}
                      className="inline-flex items-center gap-1 text-[11px] font-medium text-primary hover:underline"
                    >
                      <FlaskConical className="w-3 h-3" /> Find labs
                    </button>
                  )}
                  {canEdit && labsFor !== item.id && <LabLookupNote lookups={lookups} id={item.id} />}
                </div>
                {canEdit && labsFor === item.id && (
                  <LabSuggestions hypothesis={item} onSaved={replace} onClose={closeLabs} />
                )}
              </li>
            );
          })}
        </ul>
      )}

      {showForm && (
        <div className={`space-y-2 ${open.length ? "mt-3 pt-3 border-t border-border" : ""}`}>
          <div>
            <label htmlFor="hypothesis-title" className="text-[11px] text-muted-foreground">What might be wrong?</label>
            <Input
              id="hypothesis-title"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && save()}
              placeholder="e.g. thyroid problem"
              maxLength={240}
              className="mt-1 text-sm"
            />
          </div>
          <div>
            <label htmlFor="hypothesis-why" className="text-[11px] text-muted-foreground">Why do you think so? (optional)</label>
            <Input
              id="hypothesis-why"
              value={why}
              onChange={(event) => setWhy(event.target.value)}
              placeholder="Symptoms, labs or patterns that point to it"
              className="mt-1 text-sm"
            />
          </div>
          <div>
            <label htmlFor="hypothesis-confirm" className="text-[11px] text-muted-foreground">What would confirm or rule it out? (optional)</label>
            <Input
              id="hypothesis-confirm"
              value={confirmBy}
              onChange={(event) => setConfirmBy(event.target.value)}
              placeholder="Test, referral or specialist to ask"
              className="mt-1 text-sm"
            />
          </div>
          <div className="flex gap-2">
            <Button size="sm" onClick={save} disabled={saving || !title.trim()} className="gap-1.5">
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} Add hypothesis
            </Button>
            {adding && open.length > 0 && (
              <Button size="sm" variant="ghost" onClick={() => setAdding(false)}>Cancel</Button>
            )}
          </div>
        </div>
      )}

      {canEdit && (
        <p className="mt-3 text-[11px] text-muted-foreground">
          {decided.length > 0 && `${decided.length} decided by a clinician. `}
          Add evidence or record a clinician's decision in{" "}
          <Link to="/settings#hypotheses" className="font-medium text-primary hover:underline">
            Settings → Health hypotheses
          </Link>.
        </p>
      )}
    </div>
  );
}
