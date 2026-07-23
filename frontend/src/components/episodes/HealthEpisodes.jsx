import { useCallback, useEffect, useState } from "react";
import { CalendarRange, Check, Loader2, Plus, RefreshCw, X } from "lucide-react";
import { toast } from "sonner";

/** @param {import("react").InputHTMLAttributes<HTMLInputElement>} props */
function Input({ className = "", ...props }) {
  return <input className={`h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm ${className}`} {...props} />;
}

/** @param {import("react").ButtonHTMLAttributes<HTMLButtonElement> & {variant?: string, size?: string}} props */
function Button({ className = "", variant = "default", size = "default", ...props }) {
  const tone = variant === "outline"
    ? "border border-input bg-transparent hover:bg-accent"
    : "bg-primary text-primary-foreground hover:bg-primary/90";
  const dimensions = size === "sm" ? "h-8 px-3 text-xs" : "h-9 px-4 text-sm";
  return (
    <button
      className={`inline-flex items-center justify-center rounded-md font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 ${tone} ${dimensions} ${className}`}
      {...props}
    />
  );
}

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

function today() {
  return new Date().toISOString().slice(0, 10);
}

const EMPTY = {
  episode_type: "symptom_flare",
  title: "",
  description: "",
  start_time: today(),
  end_time: today(),
};

const STATUS_TONE = {
  proposed: "border-amber-200 bg-amber-50 text-amber-900",
  confirmed: "border-emerald-200 bg-emerald-50 text-emerald-900",
  dismissed: "border-slate-200 bg-slate-50 text-slate-700",
};

export default function HealthEpisodes() {
  const [items, setItems] = useState([]);
  const [canEdit, setCanEdit] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [candidates, setCandidates] = useState([]);
  const [selected, setSelected] = useState(new Set());

  const load = useCallback(async () => {
    try {
      const data = await api("/api/episodes?include_dismissed=true");
      setItems(data.episodes || []);
      setCanEdit(Boolean(data.can_edit));
    } catch (error) {
      toast.error(error.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function findContext() {
    try {
      const data = await api(
        `/api/episodes/candidates?start=${encodeURIComponent(form.start_time)}&end=${encodeURIComponent(form.end_time)}`,
      );
      setCandidates(data.candidates || []);
      setSelected(new Set());
    } catch (error) {
      toast.error(error.message);
    }
  }

  function toggle(id) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  async function create() {
    if (!form.title.trim() || saving) return;
    setSaving(true);
    try {
      const created = await api("/api/episodes", {
        method: "POST",
        body: JSON.stringify({
          ...form,
          origin_kind: "manual",
          members: candidates
            .filter((item) => selected.has(item.entity_id))
            .map(({
              entity_type,
              entity_id,
              role,
              relationship_kind,
              observed_start,
              observed_end,
              source_version,
              summary,
            }) => ({
              entity_type,
              entity_id,
              role,
              relationship_kind,
              observed_start,
              observed_end,
              source_version,
              summary,
            })),
        }),
      });
      setItems((current) => [created, ...current]);
      setForm({ ...EMPTY, start_time: form.end_time, end_time: form.end_time });
      setCandidates([]);
      setSelected(new Set());
      toast.success("Episode recorded as proposed");
    } catch (error) {
      toast.error(error.message);
    } finally {
      setSaving(false);
    }
  }

  async function decide(item, status) {
    const reason = window.prompt(
      status === "confirmed"
        ? "Why are you confirming these dates and members?"
        : "Why should this episode be dismissed?",
    );
    if (!reason?.trim()) return;
    try {
      const updated = await api(`/api/episodes/${item.id}/decision`, {
        method: "POST",
        body: JSON.stringify({ status, reason }),
      });
      setItems((current) => current.map((value) => value.id === updated.id ? updated : value));
    } catch (error) {
      toast.error(error.message);
    }
  }

  async function correct(item) {
    const start = window.prompt("Corrected start (ISO date or timezone-aware timestamp)", item.start_time);
    if (!start) return;
    const end = window.prompt("Corrected end (same precision as start)", item.end_time);
    if (!end) return;
    const reason = window.prompt("Why are these dates being corrected?");
    if (!reason?.trim()) return;
    try {
      const updated = await api(`/api/episodes/${item.id}`, {
        method: "PUT",
        body: JSON.stringify({ start_time: start, end_time: end, reason }),
      });
      setItems((current) => current.map((value) => value.id === updated.id ? updated : value));
    } catch (error) {
      toast.error(error.message);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading health episodes…
      </div>
    );
  }

  return (
    <section className="space-y-3" aria-labelledby="health-episodes-heading">
      <div>
        <h2 id="health-episodes-heading" className="font-semibold flex items-center gap-2">
          <CalendarRange className="h-4 w-4 text-primary" /> Health episodes
        </h2>
        <p className="text-xs text-muted-foreground mt-1">
          Group symptoms and nearby health events across a time range. Membership means temporal
          context only—it does not establish causation.
        </p>
      </div>

      {canEdit && (
        <div className="rounded-xl border border-border bg-card p-4 space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-12 gap-2">
            <Input
              aria-label="Episode title"
              value={form.title}
              onChange={(event) => setForm({ ...form, title: event.target.value })}
              placeholder="e.g. Migraine flare"
              className="sm:col-span-4"
            />
            <Input
              aria-label="Episode type"
              value={form.episode_type}
              onChange={(event) => setForm({ ...form, episode_type: event.target.value })}
              placeholder="Episode type"
              className="sm:col-span-3"
            />
            <Input
              aria-label="Episode start"
              type="date"
              value={form.start_time}
              onChange={(event) => setForm({ ...form, start_time: event.target.value })}
              className="sm:col-span-2"
            />
            <Input
              aria-label="Episode end"
              type="date"
              value={form.end_time}
              onChange={(event) => setForm({ ...form, end_time: event.target.value })}
              className="sm:col-span-2"
            />
            <Button variant="outline" onClick={findContext} className="sm:col-span-1 px-2" title="Find temporal context">
              <RefreshCw className="h-4 w-4" />
            </Button>
          </div>
          <Input
            aria-label="Episode description"
            value={form.description}
            onChange={(event) => setForm({ ...form, description: event.target.value })}
            placeholder="Optional description"
          />
          {candidates.length > 0 && (
            <div className="rounded-lg border border-border p-2">
              <p className="text-[11px] font-semibold mb-1">Choose temporally nearby records</p>
              <div className="max-h-36 overflow-y-auto space-y-1">
                {candidates.map((candidate) => (
                  <label key={`${candidate.entity_type}:${candidate.entity_id}`} className="flex gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={selected.has(candidate.entity_id)}
                      onChange={() => toggle(candidate.entity_id)}
                    />
                    <span>
                      {candidate.summary || candidate.entity_type} · {candidate.observed_start}{" "}
                      <span className="text-muted-foreground">({candidate.entity_type})</span>
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}
          <div className="flex justify-end">
            <Button size="sm" onClick={create} disabled={saving || !form.title.trim()} className="gap-1.5">
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
              Record proposed episode
            </Button>
          </div>
        </div>
      )}

      {items.length === 0 ? (
        <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
          No health episodes recorded yet.
        </p>
      ) : (
        <div className="space-y-2">
          {items.map((item) => (
            <article key={item.id} className={`rounded-lg border p-3 ${STATUS_TONE[item.status] || ""}`}>
              <div className="flex flex-wrap justify-between gap-2">
                <div>
                  <p className="font-semibold text-sm">{item.title}</p>
                  <p className="text-xs">{item.start_time} → {item.end_time} · {item.episode_type}</p>
                </div>
                <span className="text-[10px] uppercase font-semibold">{item.status} · {item.origin_kind}</span>
              </div>
              {item.description && <p className="text-xs mt-1">{item.description}</p>}
              <p className="text-[11px] mt-2">
                {item.members?.length || 0} temporal member{item.members?.length === 1 ? "" : "s"} · confidence{" "}
                {item.confidence?.confidence_label?.replace("_", " ") || "not assessed"} · not causal
              </p>
              {item.members?.length > 0 && (
                <ul className="mt-1 text-[11px] list-disc pl-4">
                  {item.members.map((member) => (
                    <li key={`${member.entity_type}:${member.entity_id}`}>
                      {member.summary || member.entity_type} · {member.relationship_kind.replaceAll("_", " ")}
                    </li>
                  ))}
                </ul>
              )}
              {canEdit && item.status === "proposed" && (
                <div className="flex flex-wrap gap-2 mt-2">
                  <Button size="sm" variant="outline" onClick={() => correct(item)}>Correct dates</Button>
                  <Button size="sm" onClick={() => decide(item, "confirmed")} className="gap-1">
                    <Check className="h-3.5 w-3.5" /> Confirm
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => decide(item, "dismissed")} className="gap-1">
                    <X className="h-3.5 w-3.5" /> Dismiss
                  </Button>
                </div>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
