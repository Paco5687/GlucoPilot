import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { TriangleAlert, Plus, X, Loader2 } from "lucide-react";
import { toast } from "sonner";

const SEV_TONE = {
  severe: "bg-rose-100 text-rose-700",
  moderate: "bg-amber-100 text-amber-700",
  mild: "bg-muted text-muted-foreground",
};

export default function AllergiesSettings() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [f, setF] = useState({ allergen: "", reaction: "", severity: "", notes: "" });
  const [saving, setSaving] = useState(false);

  async function load() {
    try { const r = await fetch("/api/allergies", { credentials: "same-origin" }); if (r.ok) setItems((await r.json()).allergies || []); } catch { /* */ }
    setLoading(false);
  }
  useEffect(() => { load(); }, []);

  async function add() {
    if (!f.allergen.trim()) return;
    setSaving(true);
    try {
      const r = await fetch("/api/allergies", { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin", body: JSON.stringify(f) });
      if (!r.ok) throw new Error("Save failed");
      setItems((await r.json()).allergies || []);
      setF({ allergen: "", reaction: "", severity: "", notes: "" });
    } catch (err) { toast.error(err.message); }
    setSaving(false);
  }
  async function remove(id) {
    const r = await fetch(`/api/allergies/${id}`, { method: "DELETE", credentials: "same-origin" });
    if (r.ok) setItems((await r.json()).allergies || []);
  }

  if (loading) return null;

  return (
    <div className="bg-card rounded-xl border border-border p-5 space-y-4">
      <div className="flex items-center gap-2">
        <TriangleAlert className="w-5 h-5 text-primary" />
        <div>
          <h3 className="font-semibold text-sm">Allergies</h3>
          <p className="text-xs text-muted-foreground">Drug, food, or environmental. Shown on the Visit Report and known to the Companion.</p>
        </div>
      </div>

      {items.length > 0 && (
        <div className="space-y-1.5">
          {items.map((a) => (
            <div key={a.id} className="group flex items-center gap-2 text-sm bg-muted/40 rounded-lg px-3 py-1.5">
              <span className="font-medium">{a.allergen}</span>
              {a.severity && <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${SEV_TONE[a.severity] || "bg-muted text-muted-foreground"}`}>{a.severity}</span>}
              {a.reaction && <span className="text-xs text-muted-foreground">· {a.reaction}</span>}
              {a.notes && <span className="text-xs text-muted-foreground truncate">· {a.notes}</span>}
              <button onClick={() => remove(a.id)} className="ml-auto opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive"><X className="w-3.5 h-3.5" /></button>
            </div>
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-12 gap-2 items-end">
        <div className="sm:col-span-4">
          <label className="text-[11px] text-muted-foreground">Allergen</label>
          <Input value={f.allergen} onChange={(e) => setF({ ...f, allergen: e.target.value })} onKeyDown={(e) => e.key === "Enter" && add()} placeholder="e.g. Penicillin" className="mt-1 text-sm" />
        </div>
        <div className="sm:col-span-4">
          <label className="text-[11px] text-muted-foreground">Reaction</label>
          <Input value={f.reaction} onChange={(e) => setF({ ...f, reaction: e.target.value })} placeholder="e.g. hives" className="mt-1 text-sm" />
        </div>
        <div className="sm:col-span-2">
          <label className="text-[11px] text-muted-foreground">Severity</label>
          <select value={f.severity} onChange={(e) => setF({ ...f, severity: e.target.value })} className="mt-1 w-full h-9 rounded-md border border-border bg-background px-2 text-sm">
            <option value="">—</option>
            <option value="mild">mild</option>
            <option value="moderate">moderate</option>
            <option value="severe">severe</option>
          </select>
        </div>
        <div className="sm:col-span-2">
          <Button size="sm" onClick={add} disabled={saving || !f.allergen.trim()} className="w-full gap-1.5">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} Add
          </Button>
        </div>
      </div>
    </div>
  );
}
