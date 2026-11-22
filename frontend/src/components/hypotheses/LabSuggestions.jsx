import { useCallback, useEffect, useState } from "react";
import { FlaskConical, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";

// Lab results that bear on one hypothesis, for the owner to pick from. The
// server asks the local model which tests matter and fills in every value,
// date and flag from the lab records; nothing is saved until the owner picks
// items and a role for each.

const ROLE_LABEL = { supporting: "Supports", opposing: "Against" };
const FLAG_TONE = {
  high: "bg-rose-100 text-rose-800",
  low: "bg-blue-100 text-blue-800",
  normal: "bg-emerald-100 text-emerald-800",
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

function flagTone(flag) {
  if (["high", "h", "hh", "critical_high"].includes(flag)) return FLAG_TONE.high;
  if (["low", "l", "ll", "critical_low"].includes(flag)) return FLAG_TONE.low;
  if (flag === "normal") return FLAG_TONE.normal;
  return "bg-muted text-muted-foreground";
}

function LabRow({ item, choice, onChange }) {
  const disabled = item.already_added;
  return (
    <li className="rounded-md border border-border bg-background px-2.5 py-2 text-xs space-y-1">
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="checkbox"
          aria-label={`Add ${item.test}`}
          checked={Boolean(choice.checked)}
          disabled={disabled}
          onChange={(event) => onChange({ ...choice, checked: event.target.checked })}
        />
        <span className="font-medium text-sm">{item.test}</span>
        <span>{item.value}</span>
        <span className="text-muted-foreground">{item.date}</span>
        {item.flag && <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${flagTone(item.flag)}`}>{item.flag}</span>}
        {item.reference && <span className="text-muted-foreground">ref {item.reference}</span>}
        <span className="ml-auto">
          {disabled ? (
            <span className="text-muted-foreground">Already in evidence</span>
          ) : (
            <select
              aria-label={`Role for ${item.test}`}
              value={choice.role || ""}
              onChange={(event) => onChange({ ...choice, role: event.target.value, checked: choice.checked || Boolean(event.target.value) })}
              className="h-7 rounded-md border border-border bg-background px-1.5 text-xs"
            >
              <option value="">Choose…</option>
              <option value="supporting">Supports</option>
              <option value="opposing">Against</option>
            </select>
          )}
        </span>
      </div>
      {item.earlier?.length > 0 && (
        <p className="text-muted-foreground">
          Earlier: {item.earlier.map((e) => `${e.value} on ${e.date}`).join("; ")}
        </p>
      )}
      <p className="text-muted-foreground">
        {item.if_true === "any" ? "No usual direction." : `Usually ${item.if_true} if this is true.`} {item.why}
      </p>
      {item.other_cause && (
        <p className="text-amber-800">Also affected by {item.other_cause}, so the direction alone doesn't decide it.</p>
      )}
      {!disabled && item.suggested_role && (
        <p className="text-muted-foreground">Suggested: {ROLE_LABEL[item.suggested_role]}.</p>
      )}
    </li>
  );
}

export default function LabSuggestions({ hypothesis, onSaved, onClose }) {
  const [state, setState] = useState({ loading: true, error: "", result: null });
  const [choices, setChoices] = useState({});
  const [saving, setSaving] = useState(false);

  const run = useCallback(async () => {
    setState({ loading: true, error: "", result: null });
    try {
      const result = await api(`/api/hypotheses/${hypothesis.id}/lab-suggestions`, { method: "POST" });
      const initial = {};
      // Nothing starts ticked: the owner decides what goes into the record.
      for (const item of result.labs || []) initial[`lab:${item.evidence.source_id}`] = { checked: false, role: item.suggested_role || "" };
      for (const item of result.not_on_file || []) initial[`missing:${item.test}`] = { checked: false };
      setChoices(initial);
      setState({ loading: false, error: "", result });
    } catch (error) {
      setState({ loading: false, error: error.message, result: null });
    }
  }, [hypothesis.id]);

  useEffect(() => { run(); }, [run]);

  const result = state.result;
  const labs = result?.labs || [];
  const missing = result?.not_on_file || [];
  const picked = [
    ...labs
      .filter((item) => choices[`lab:${item.evidence.source_id}`]?.checked)
      .map((item) => ({ ...item.evidence, role: choices[`lab:${item.evidence.source_id}`].role })),
    ...missing.filter((item) => choices[`missing:${item.test}`]?.checked).map((item) => item.evidence),
  ];
  const needsRole = picked.some((item) => !item.role);

  async function save() {
    if (!picked.length || needsRole) return;
    setSaving(true);
    try {
      // A revision replaces the whole evidence list, so build on the stored
      // one, never a copy that may be stale.
      const current = await api(`/api/hypotheses/${hypothesis.id}`);
      const updated = await api(`/api/hypotheses/${hypothesis.id}/evidence`, {
        method: "PUT",
        body: JSON.stringify({
          reason: `Added ${picked.length} lab item${picked.length === 1 ? "" : "s"} suggested by ${result.method}; the owner chose each item and its role.`,
          evidence: [...(current.evidence || []), ...picked],
        }),
      });
      toast.success(`Added ${picked.length} to evidence`);
      onSaved?.(updated);
      onClose?.();
    } catch (error) {
      toast.error(error.message);
    }
    setSaving(false);
  }

  return (
    <div className="rounded-lg border border-border bg-muted/30 p-3 space-y-2">
      <div className="flex items-center gap-1.5 text-xs font-semibold">
        <FlaskConical className="w-3.5 h-3.5 text-primary" /> Lab results for “{hypothesis.title}”
      </div>

      {state.loading && (
        <p className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="w-3.5 h-3.5 animate-spin" /> Checking lab results…
        </p>
      )}

      {state.error && (
        <div className="flex items-center gap-2 text-xs">
          <span className="text-destructive">{state.error}</span>
          <Button size="sm" variant="outline" onClick={() => run()}>Retry</Button>
        </div>
      )}

      {result && result.lab_tests_used === false && (
        <p className="text-xs">
          Usually diagnosed by {result.usually_checked_by || "history and examination"}, not lab tests
          {labs.length || missing.length ? ", so these results may not settle it." : "."}
        </p>
      )}

      {result && result.lab_tests_used !== false && !labs.length && !missing.length && (
        <p className="text-xs text-muted-foreground">No related lab tests found.</p>
      )}

      {labs.length > 0 && (
        <ul className="space-y-1.5">
          {labs.map((item) => {
            const key = `lab:${item.evidence.source_id}`;
            return (
              <LabRow
                key={key}
                item={item}
                choice={choices[key] || {}}
                onChange={(choice) => setChoices((current) => ({ ...current, [key]: choice }))}
              />
            );
          })}
        </ul>
      )}

      {missing.length > 0 && (
        <div className="space-y-1">
          <p className="text-[11px] font-semibold text-muted-foreground">Not on file</p>
          <ul className="space-y-1">
            {missing.map((item) => {
              const key = `missing:${item.test}`;
              return (
                <li key={key} className="flex items-start gap-2 text-xs">
                  <input
                    type="checkbox"
                    aria-label={`Add ${item.test} as still needed`}
                    className="mt-0.5"
                    checked={Boolean(choices[key]?.checked)}
                    disabled={item.already_added}
                    onChange={(event) => setChoices((current) => ({ ...current, [key]: { checked: event.target.checked } }))}
                  />
                  <span>
                    <span className="font-medium">{item.test}</span>
                    {item.already_added ? " · already listed as needed" : item.why ? ` · ${item.why}` : ""}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {result && (labs.length > 0 || missing.length > 0) && (
        <p className="text-[11px] text-muted-foreground">
          The local model picked these tests{result.looked_up_at ? ` on ${result.looked_up_at.slice(0, 10)}` : ""}.
          {" "}Values, dates and flags come from your lab records.
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {(labs.length > 0 || missing.length > 0) && (
          <Button size="sm" onClick={save} disabled={saving || !picked.length || needsRole} className="gap-1.5">
            {saving && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            {picked.length ? `Add ${picked.length} to evidence` : "Add to evidence"}
          </Button>
        )}
        {needsRole && <span className="text-[11px] text-amber-800">Choose Supports or Against for each ticked lab.</span>}
        <Button size="sm" variant="ghost" onClick={onClose}>Close</Button>
      </div>
    </div>
  );
}
