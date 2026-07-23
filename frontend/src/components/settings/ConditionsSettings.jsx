import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Stethoscope, Plus, X, Loader2 } from "lucide-react";
import { toast } from "sonner";

const STATUS_TONE = {
  active: "bg-rose-100 text-rose-700",
  resolved: "bg-emerald-100 text-emerald-700",
  suspected: "bg-amber-100 text-amber-700",
};

export default function ConditionsSettings() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [name, setName] = useState("");
  const [date, setDate] = useState("");
  const [status, setStatus] = useState("active");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);

  async function load() {
    try {
      const r = await fetch("/api/conditions", { credentials: "same-origin" });
      if (r.ok) setItems((await r.json()).conditions || []);
    } catch { /* */ }
    setLoading(false);
  }
  useEffect(() => { load(); }, []);

  async function add() {
    if (!name.trim()) return;
    setSaving(true);
    try {
      const r = await fetch("/api/conditions", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify({ name: name.trim(), diagnosed_date: date, status, notes: notes.trim() }),
      });
      if (!r.ok) throw new Error("Save failed");
      setItems((await r.json()).conditions || []);
      setName(""); setDate(""); setNotes(""); setStatus("active");
    } catch (err) { toast.error(err.message || "Save failed"); }
    setSaving(false);
  }

  async function remove(id) {
    const r = await fetch(`/api/conditions/${id}`, { method: "DELETE", credentials: "same-origin" });
    if (r.ok) setItems((await r.json()).conditions || []);
  }

  if (loading) return null;
  const confirmedItems = items.filter((item) => item.status !== "suspected");
  const legacySuspected = items.filter((item) => item.status === "suspected");

  return (
    <div className="bg-card rounded-xl border border-border p-5 space-y-4">
      <div className="flex items-center gap-2">
        <Stethoscope className="w-5 h-5 text-primary" />
        <div>
          <h3 className="font-semibold text-sm">Confirmed conditions &amp; diagnoses</h3>
          <p className="text-xs text-muted-foreground">Only established diagnoses belong here. Tentative ideas belong in the separate hypothesis ledger below.</p>
        </div>
      </div>

      {confirmedItems.length > 0 && (
        <div className="space-y-1.5">
          {confirmedItems.map((c) => (
            <div key={c.id} className="group flex items-center gap-2 text-sm bg-muted/40 rounded-lg px-3 py-1.5">
              <span className="font-medium">{c.name}</span>
              <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${STATUS_TONE[c.status] || "bg-muted text-muted-foreground"}`}>{c.status}</span>
              {c.diagnosed_date && <span className="text-xs text-muted-foreground">dx {c.diagnosed_date}</span>}
              {c.notes && <span className="text-xs text-muted-foreground truncate">· {c.notes}</span>}
              <button onClick={() => remove(c.id)} className="ml-auto opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive"><X className="w-3.5 h-3.5" /></button>
            </div>
          ))}
        </div>
      )}

      {legacySuspected.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 space-y-1.5">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-amber-900">
            Legacy suspected entries · hypotheses, not diagnoses
          </p>
          <p className="text-xs text-amber-800">
            Re-enter these in the governed hypothesis ledger below to add evidence and review history.
          </p>
          {legacySuspected.map((c) => (
            <div key={c.id} className="group flex items-center gap-2 text-sm">
              <span className="font-medium">{c.name}</span>
              {c.notes && <span className="text-xs text-muted-foreground truncate">· {c.notes}</span>}
              <button onClick={() => remove(c.id)} className="ml-auto opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive"><X className="w-3.5 h-3.5" /></button>
            </div>
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-12 gap-2 items-end">
        <div className="sm:col-span-5">
          <label className="text-[11px] text-muted-foreground">Condition</label>
          <Input value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()} placeholder="e.g. Hashimoto's thyroiditis" className="mt-1 text-sm" />
        </div>
        <div className="sm:col-span-3">
          <label className="text-[11px] text-muted-foreground">Diagnosed</label>
          <Input type="date" value={date} onChange={(e) => setDate(e.target.value)} className="mt-1 text-sm" />
        </div>
        <div className="sm:col-span-2">
          <label className="text-[11px] text-muted-foreground">Status</label>
          <select value={status} onChange={(e) => setStatus(e.target.value)} className="mt-1 w-full h-9 rounded-md border border-border bg-background px-2 text-sm">
            <option value="active">active</option>
            <option value="resolved">resolved</option>
          </select>
        </div>
        <div className="sm:col-span-2">
          <Button size="sm" onClick={add} disabled={saving || !name.trim()} className="w-full gap-1.5">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} Add
          </Button>
        </div>
      </div>
      <Input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Optional note (e.g. managed with levothyroxine)" className="text-sm" />
    </div>
  );
}
