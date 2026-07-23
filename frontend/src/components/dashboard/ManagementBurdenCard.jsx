import { useCallback, useEffect, useMemo, useState } from "react";
import { ClipboardCheck, Loader2, Plus, RefreshCw } from "lucide-react";
import { toast } from "sonner";

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

const CATEGORIES = [
  "bolus", "override", "temp_basal", "pump_interaction", "fingerstick",
  "ketone", "rescue_carbs", "awakening", "device_change",
  "activity_for_control", "other",
];

function localInput(date = new Date()) {
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

export default function ManagementBurdenCard({ days = 90 }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({
    occurred_at: localInput(),
    category: "other",
    duration_minutes: 3,
    notes: "",
  });

  const load = useCallback(async () => {
    try {
      setData(await api(`/api/management-burden?days=${days}`));
    } catch (error) {
      toast.error(error.message);
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    let active = true;
    api(`/api/management-burden?days=${days}`)
      .then((result) => { if (active) setData(result); })
      .catch((error) => { if (active) toast.error(error.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [days]);

  const missing = data?.source_coverage?.missing || [];
  const componentTotal = useMemo(
    () => (data?.components || []).reduce((sum, item) => sum + Number(item.events || 0), 0),
    [data],
  );

  async function createEvent() {
    if (saving) return;
    setSaving(true);
    try {
      await api("/api/management-burden/events", {
        method: "POST",
        body: JSON.stringify({
          ...form,
          occurred_at: new Date(form.occurred_at).toISOString(),
          duration_minutes: Number(form.duration_minutes),
          interaction_count: 1,
        }),
      });
      setShowForm(false);
      setForm({ occurred_at: localInput(), category: "other", duration_minutes: 3, notes: "" });
      await load();
      toast.success("Management-effort event recorded");
    } catch (error) {
      toast.error(error.message);
    } finally {
      setSaving(false);
    }
  }

  async function correctEvent(event) {
    const duration = window.prompt("Corrected active-management minutes", event.duration_minutes);
    if (duration == null) return;
    const reason = window.prompt("Why is this event being corrected?");
    if (!reason?.trim()) return;
    const exclude = window.confirm("Exclude this event from the calculated burden metrics?");
    try {
      await api(`/api/management-burden/events/${encodeURIComponent(event.original_event_id)}/corrections`, {
        method: "POST",
        body: JSON.stringify({
          duration_minutes: Number(duration),
          excluded: exclude,
          reason,
          notes: event.notes || "",
        }),
      });
      await load();
      toast.success("Correction appended; the source event was retained");
    } catch (error) {
      toast.error(error.message);
    }
  }

  if (loading) {
    return <div className="h-36 flex items-center justify-center"><Loader2 className="h-5 w-5 animate-spin text-primary" /></div>;
  }
  if (!data) return null;

  return (
    <section className="bg-card rounded-xl border border-border p-5 space-y-4" aria-labelledby="management-burden-heading">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 id="management-burden-heading" className="font-semibold flex items-center gap-2">
            <ClipboardCheck className="h-4 w-4 text-primary" /> Management effort
          </h2>
          <p className="text-xs text-muted-foreground mt-1 max-w-3xl">
            Recorded work is shown separately from glucose outcomes. Missing sources lower
            confidence; they are not treated as zero effort. These are descriptive calculations,
            not causal conclusions or treatment advice.
          </p>
        </div>
        <div className="flex gap-2">
          {data.can_edit && (
            <button className="h-8 px-3 rounded-md border text-xs flex items-center gap-1" onClick={() => setShowForm(!showForm)}>
              <Plus className="h-3.5 w-3.5" /> Add event
            </button>
          )}
          <button className="h-8 px-3 rounded-md border text-xs flex items-center gap-1" onClick={load}>
            <RefreshCw className="h-3.5 w-3.5" /> Refresh
          </button>
        </div>
      </div>

      {showForm && (
        <div className="grid grid-cols-1 md:grid-cols-5 gap-2 rounded-lg border p-3">
          <input aria-label="Effort event time" type="datetime-local" value={form.occurred_at} onChange={(event) => setForm({ ...form, occurred_at: event.target.value })} className="h-9 rounded-md border bg-transparent px-2" />
          <select aria-label="Effort category" value={form.category} onChange={(event) => setForm({ ...form, category: event.target.value })} className="h-9 rounded-md border bg-background px-2">
            {CATEGORIES.map((category) => <option key={category} value={category}>{category.replaceAll("_", " ")}</option>)}
          </select>
          <input aria-label="Active management minutes" type="number" min="0" max="1440" value={form.duration_minutes} onChange={(event) => setForm({ ...form, duration_minutes: Number(event.target.value) })} className="h-9 rounded-md border bg-transparent px-2" />
          <input aria-label="Effort notes" value={form.notes} onChange={(event) => setForm({ ...form, notes: event.target.value })} placeholder="Optional notes" className="h-9 rounded-md border bg-transparent px-2" />
          <button disabled={saving} onClick={createEvent} className="h-9 rounded-md bg-primary text-primary-foreground text-sm">{saving ? "Saving…" : "Record"}</button>
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-sm">
        <div className="rounded-lg border p-3"><b>{data.outcomes.time_in_range_pct ?? "—"}%</b><br /><span className="text-xs text-muted-foreground">time in range</span></div>
        <div className="rounded-lg border p-3"><b>{data.summary.measured_effort_index}</b><br /><span className="text-xs text-muted-foreground">measured effort index</span></div>
        <div className="rounded-lg border p-3"><b>{data.summary.average_active_management_minutes_per_day}</b><br /><span className="text-xs text-muted-foreground">active min/day</span></div>
        <div className="rounded-lg border p-3"><b>{data.summary.measured_interactions_per_day}</b><br /><span className="text-xs text-muted-foreground">interactions/day</span></div>
        <div className="rounded-lg border p-3"><b>{Math.round(data.analytics_confidence.confidence_score * 100)}%</b><br /><span className="text-xs text-muted-foreground">{data.analytics_confidence.confidence_label} confidence</span></div>
      </div>

      {data.outcome_vs_effort.sustainability_review_flag && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 text-amber-950 p-3 text-sm">
          {data.outcome_vs_effort.language}
        </div>
      )}
      {missing.length > 0 && (
        <p className="text-xs text-amber-700">
          Confidence reduced because these event sources are unavailable: {missing.join(", ").replaceAll("_", " ")}.
        </p>
      )}

      <div>
        <p className="text-xs font-medium mb-2">Visible score components ({componentTotal} effective events)</p>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-xs">
          {data.components.map((item) => (
            <div key={item.category} className="rounded border p-2">
              <b className="capitalize">{item.category.replaceAll("_", " ")}</b>
              <p>{item.events} events · {item.minutes} min</p>
              <p className="text-muted-foreground">weight {item.weight} · {item.weighted_points} points</p>
            </div>
          ))}
        </div>
      </div>

      {data.can_edit && data.events.length > 0 && (
        <details>
          <summary className="text-xs cursor-pointer">Audit recent events and append corrections</summary>
          <div className="mt-2 max-h-48 overflow-auto divide-y text-xs">
            {data.events.slice(0, 30).map((event) => (
              <button key={event.id} onClick={() => correctEvent(event)} className="w-full text-left py-2 hover:bg-muted/40">
                <span className="capitalize font-medium">{event.category.replaceAll("_", " ")}</span>
                {" · "}{event.duration_minutes} min · {event.origin_kind}
                {event.corrected_by ? " · corrected" : ""}{!event.effective ? " · excluded" : ""}
              </button>
            ))}
          </div>
        </details>
      )}
    </section>
  );
}
