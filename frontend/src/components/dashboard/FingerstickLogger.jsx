import { useState, useEffect, useCallback } from "react";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Droplet, Plus, Loader2 } from "lucide-react";
import { toast } from "sonner";

function localNow() {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16); // yyyy-MM-ddTHH:mm for datetime-local
}

export default function FingerstickLogger({ onAdded }) {
  const [value, setValue] = useState("");
  const [when, setWhen] = useState(localNow);
  const [saving, setSaving] = useState(false);
  const [stats, setStats] = useState(null);

  const loadStats = useCallback(async () => {
    try {
      const res = await base44.functions.invoke("fingerstick", { action: "stats" });
      setStats(res.data);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => { loadStats(); }, [loadStats]);

  async function handleAdd() {
    const v = parseFloat(value);
    if (!v || v < 10 || v > 800) { toast.error("Enter a value between 10 and 800."); return; }
    setSaving(true);
    try {
      const ts = when ? new Date(when).toISOString() : undefined;
      const res = await base44.functions.invoke("fingerstick", { action: "add", value: v, timestamp: ts });
      const r = res.data?.reading;
      if (r?.cgm_value != null) {
        const d = r.delta;
        toast.success(`Logged ${v} — CGM read ${r.cgm_value} (Δ ${d > 0 ? "+" : ""}${d})`);
      } else {
        toast.success(`Logged fingerstick ${v} mg/dL`);
      }
      setValue("");
      setWhen(localNow());
      loadStats();
      onAdded?.();
    } catch (err) {
      toast.error(err?.response?.data?.error || err.message || "Failed to log");
    }
    setSaving(false);
  }

  const biasLabel = stats?.bias === "cgm_high" ? "CGM reads high" : stats?.bias === "cgm_low" ? "CGM reads low" : "balanced";

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center gap-2 mb-3">
        <Droplet className="w-4 h-4 text-fuchsia-600" />
        <h3 className="text-sm font-semibold">Fingerstick</h3>
        <span className="text-[10px] text-muted-foreground">manual correction point</span>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <div>
          <label className="text-[11px] text-muted-foreground">Value (mg/dL)</label>
          <Input value={value} onChange={(e) => setValue(e.target.value)} inputMode="numeric" placeholder="e.g. 112"
            className="mt-1 w-28 text-sm" onKeyDown={(e) => e.key === "Enter" && handleAdd()} />
        </div>
        <div>
          <label className="text-[11px] text-muted-foreground">When</label>
          <Input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} className="mt-1 text-sm" />
        </div>
        <Button size="sm" onClick={handleAdd} disabled={saving} className="gap-1.5">
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} Log
        </Button>
      </div>

      {stats?.paired > 0 && (
        <div className="mt-3 pt-3 border-t border-border text-xs text-muted-foreground flex flex-wrap gap-x-4 gap-y-1">
          <span>CGM vs meter over <b>{stats.paired}</b> checks:</span>
          <span>avg gap <b className="text-foreground">{stats.mean_abs_delta}</b> mg/dL</span>
          <span>largest <b className="text-foreground">{stats.max_abs_delta}</b></span>
          <span>bias <b className="text-foreground">{biasLabel}</b> ({stats.mean_delta > 0 ? "+" : ""}{stats.mean_delta})</span>
        </div>
      )}
    </div>
  );
}
