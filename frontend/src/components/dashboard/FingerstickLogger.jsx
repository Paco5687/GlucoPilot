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
  const [showContext, setShowContext] = useState(false);
  const [context, setContext] = useState({
    timing_context: "unknown",
    sensor_day: "",
    sensor_site: "unknown",
    activity: "unknown",
    position: "unknown",
    hydration: "unknown",
    compression_possible: "",
    context_note: "",
  });

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
      const res = await base44.functions.invoke("fingerstick", {
        action: "add",
        value: v,
        timestamp: ts,
        ...context,
        compression_possible: context.compression_possible === ""
          ? null
          : context.compression_possible === "yes",
      });
      const r = res.data?.reading;
      if (r?.cgm_value != null) {
        const d = r.delta;
        toast.success(`Logged ${v} — CGM read ${r.cgm_value} (Δ ${d > 0 ? "+" : ""}${d})`);
      } else {
        toast.success(`Logged fingerstick ${v} mg/dL`);
      }
      setValue("");
      setWhen(localNow());
      setContext((current) => ({ ...current, context_note: "" }));
      loadStats();
      onAdded?.();
    } catch (err) {
      toast.error(err?.response?.data?.error || err.message || "Failed to log");
    }
    setSaving(false);
  }

  const biasLabel = {
    cgm_high: "CGM-high direction observed",
    cgm_low: "CGM-low direction observed",
    not_detected: "no persistent direction detected",
    insufficient_sample: "more checks needed",
  }[stats?.persistent_bias?.classification] || "not assessed";

  const setContextField = (field) => (event) => {
    setContext((current) => ({ ...current, [field]: event.target.value }));
  };

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center gap-2 mb-3">
        <Droplet className="w-4 h-4 text-fuchsia-600" />
        <h3 className="text-sm font-semibold">Fingerstick</h3>
        <span className="text-[10px] text-muted-foreground">separate meter observation</span>
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
      <button
        type="button"
        className="mt-2 text-[11px] font-medium text-primary hover:underline"
        onClick={() => setShowContext((visible) => !visible)}
        aria-expanded={showContext}
      >
        {showContext ? "Hide context" : "Add context (optional)"}
      </button>
      {showContext && (
        <div className="mt-2 grid grid-cols-2 sm:grid-cols-4 gap-2 rounded-lg border border-border bg-muted/30 p-3">
          <label className="text-[11px] text-muted-foreground">
            Timing
            <select value={context.timing_context} onChange={setContextField("timing_context")} className="mt-1 w-full rounded-md border border-input bg-background px-2 py-1.5 text-xs text-foreground">
              <option value="unknown">Not specified</option>
              <option value="waking">Waking</option>
              <option value="pre_meal">Before meal</option>
              <option value="post_meal">After meal</option>
              <option value="overnight">Overnight</option>
              <option value="exercise">Around exercise</option>
              <option value="symptoms">Symptoms</option>
              <option value="other">Other</option>
            </select>
          </label>
          <label className="text-[11px] text-muted-foreground">
            Sensor day
            <input type="number" min="1" max="30" value={context.sensor_day} onChange={setContextField("sensor_day")} className="mt-1 h-8 w-full rounded-md border border-input bg-background px-2 text-xs text-foreground" placeholder="1–30" />
          </label>
          <label className="text-[11px] text-muted-foreground">
            Sensor site
            <select value={context.sensor_site} onChange={setContextField("sensor_site")} className="mt-1 w-full rounded-md border border-input bg-background px-2 py-1.5 text-xs text-foreground">
              <option value="unknown">Not specified</option>
              <option value="arm">Arm</option>
              <option value="abdomen">Abdomen</option>
              <option value="other">Other</option>
            </select>
          </label>
          <label className="text-[11px] text-muted-foreground">
            Activity
            <select value={context.activity} onChange={setContextField("activity")} className="mt-1 w-full rounded-md border border-input bg-background px-2 py-1.5 text-xs text-foreground">
              <option value="unknown">Not specified</option>
              <option value="resting">Resting</option>
              <option value="light">Light</option>
              <option value="moderate">Moderate</option>
              <option value="vigorous">Vigorous</option>
            </select>
          </label>
          <label className="text-[11px] text-muted-foreground">
            Position
            <select value={context.position} onChange={setContextField("position")} className="mt-1 w-full rounded-md border border-input bg-background px-2 py-1.5 text-xs text-foreground">
              <option value="unknown">Not specified</option>
              <option value="upright">Standing</option>
              <option value="seated">Seated</option>
              <option value="lying">Lying down</option>
            </select>
          </label>
          <label className="text-[11px] text-muted-foreground">
            Hydration
            <select value={context.hydration} onChange={setContextField("hydration")} className="mt-1 w-full rounded-md border border-input bg-background px-2 py-1.5 text-xs text-foreground">
              <option value="unknown">Not specified</option>
              <option value="low">Possibly low</option>
              <option value="usual">Usual</option>
              <option value="high">Higher than usual</option>
            </select>
          </label>
          <label className="text-[11px] text-muted-foreground">
            Compression possible
            <select value={context.compression_possible} onChange={setContextField("compression_possible")} className="mt-1 w-full rounded-md border border-input bg-background px-2 py-1.5 text-xs text-foreground">
              <option value="">Not specified</option>
              <option value="yes">Yes</option>
              <option value="no">No</option>
            </select>
          </label>
          <label className="text-[11px] text-muted-foreground col-span-2 sm:col-span-1">
            Note
            <input maxLength={500} value={context.context_note} onChange={setContextField("context_note")} className="mt-1 h-8 w-full rounded-md border border-input bg-background px-2 text-xs text-foreground" placeholder="Optional circumstances" />
          </label>
        </div>
      )}

      {stats?.paired > 0 && (
        <div className="mt-3 pt-3 border-t border-border text-xs text-muted-foreground flex flex-wrap gap-x-4 gap-y-1">
          <span>CGM vs meter over <b>{stats.paired}</b> checks:</span>
          <span>avg gap <b className="text-foreground">{stats.mean_abs_delta}</b> mg/dL</span>
          <span>largest <b className="text-foreground">{stats.max_abs_delta}</b></span>
          <span>direction <b className="text-foreground">{biasLabel}</b> ({stats.persistent_bias?.sample_count || 0} checks)</span>
        </div>
      )}
    </div>
  );
}
