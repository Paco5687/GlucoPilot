import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Pill, Plus, X, Loader2 } from "lucide-react";
import { toast } from "sonner";

export default function MedicationsSettings() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [f, setF] = useState({ name: "", kind: "medication", dose: "", frequency: "", notes: "" });
  const [saving, setSaving] = useState(false);

  async function load() {
    try { const r = await fetch("/api/medications", { credentials: "same-origin" }); if (r.ok) setItems((await r.json()).medications || []); } catch { /* */ }
    setLoading(false);
  }
  useEffect(() => { load(); }, []);

  async function add() {
    if (!f.name.trim()) return;
    setSaving(true);
    try {
      const r = await fetch("/api/medications", { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin", body: JSON.stringify(f) });
      if (!r.ok) throw new Error("Save failed");
      setItems((await r.json()).medications || []);
      setF({ name: "", kind: f.kind, dose: "", frequency: "", notes: "" });
    } catch (err) { toast.error(err.message); }
    setSaving(false);
  }
  async function remove(id) {
    const r = await fetch(`/api/medications/${id}`, { method: "DELETE", credentials: "same-origin" });
    if (r.ok) setItems((await r.json()).medications || []);
  }

  if (loading) return null;
  const meds = items.filter((m) => m.kind !== "supplement");
  const supps = items.filter((m) => m.kind === "supplement");

  const Row = (m) => (
    <div key={m.id} className="group flex items-center gap-2 text-sm bg-muted/40 rounded-lg px-3 py-1.5">
      <span className="font-medium">{m.name}</span>
      {m.dose && <span className="text-xs text-muted-foreground">{m.dose}</span>}
      {m.frequency && <span className="text-xs text-muted-foreground">· {m.frequency}</span>}
      {m.status === "stopped" && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground">stopped</span>}
      {m.notes && <span className="text-xs text-muted-foreground truncate">· {m.notes}</span>}
      <button onClick={() => remove(m.id)} className="ml-auto opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive"><X className="w-3.5 h-3.5" /></button>
    </div>
  );

  return (
    <div className="bg-card rounded-xl border border-border p-5 space-y-4">
      <div className="flex items-center gap-2">
        <Pill className="w-5 h-5 text-primary" />
        <div>
          <h3 className="font-semibold text-sm">Medications &amp; supplements</h3>
          <p className="text-xs text-muted-foreground">What Emily takes. Feeds the Companion, Overview, and Visit Report.</p>
        </div>
      </div>

      {meds.length > 0 && <div className="space-y-1.5">{meds.map(Row)}</div>}
      {supps.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Supplements</p>
          {supps.map(Row)}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-12 gap-2 items-end">
        <div className="sm:col-span-4">
          <label className="text-[11px] text-muted-foreground">Name</label>
          <Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} onKeyDown={(e) => e.key === "Enter" && add()} placeholder="e.g. Levothyroxine" className="mt-1 text-sm" />
        </div>
        <div className="sm:col-span-2">
          <label className="text-[11px] text-muted-foreground">Type</label>
          <select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })} className="mt-1 w-full h-9 rounded-md border border-border bg-background px-2 text-sm">
            <option value="medication">medication</option>
            <option value="supplement">supplement</option>
          </select>
        </div>
        <div className="sm:col-span-2">
          <label className="text-[11px] text-muted-foreground">Dose</label>
          <Input value={f.dose} onChange={(e) => setF({ ...f, dose: e.target.value })} placeholder="50 mcg" className="mt-1 text-sm" />
        </div>
        <div className="sm:col-span-2">
          <label className="text-[11px] text-muted-foreground">Frequency</label>
          <Input value={f.frequency} onChange={(e) => setF({ ...f, frequency: e.target.value })} placeholder="daily" className="mt-1 text-sm" />
        </div>
        <div className="sm:col-span-2">
          <Button size="sm" onClick={add} disabled={saving || !f.name.trim()} className="w-full gap-1.5">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} Add
          </Button>
        </div>
      </div>
      <Input value={f.notes} onChange={(e) => setF({ ...f, notes: e.target.value })} placeholder="Optional note (e.g. for Hashimoto's; taken at bedtime)" className="text-sm" />
    </div>
  );
}
