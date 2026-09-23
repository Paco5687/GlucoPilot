import { useState, useEffect, useCallback, useRef } from "react";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Utensils, ScanBarcode, Camera, Star, Plus, Minus, Loader2, Trash2, X } from "lucide-react";
import { toast } from "sonner";
import BarcodeScanner from "./BarcodeScanner";

function localNow() {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}

function fileToImageString(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      const comma = dataUrl.indexOf(",");
      const media = dataUrl.slice(5, dataUrl.indexOf(";")) || "image/jpeg";
      resolve(`${media}|${dataUrl.slice(comma + 1)}`);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

const EMPTY = { name: "", brand: "", serving_label: "1 serving", carbs: "", fiber: "", sugars: "", protein: "", fat: "", calories: "", servings: 1, savePreset: true, preset_id: null, barcode: null, source: "manual" };

export default function FoodLogger({ onLogged }) {
  const [presets, setPresets] = useState([]);
  const [recent, setRecent] = useState([]);
  const [mode, setMode] = useState(null); // null | "scan" | "confirm"
  const [draft, setDraft] = useState(EMPTY);
  const [when, setWhen] = useState(localNow);
  const [busy, setBusy] = useState(false);
  const [servingsById, setServingsById] = useState({});
  const fileRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const r = await base44.functions.invoke("food", { action: "presets" });
      setPresets(r.data?.presets || []);
      setRecent(r.data?.recent || []);
    } catch { /* ignore */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  function openConfirm(fields) {
    setDraft({ ...EMPTY, ...fields });
    setWhen(localNow());
    setMode("confirm");
  }

  const onBarcode = useCallback(async (code) => {
    setMode(null);
    setBusy(true);
    try {
      const r = await base44.functions.invoke("food", { action: "lookup_barcode", barcode: code });
      const p = r.data?.product;
      if (!p) throw new Error(r.data?.error || "Lookup failed");
      openConfirm({ name: p.name || "", brand: p.brand || "", serving_label: p.serving_label, carbs: p.carbs ?? "", fiber: p.fiber ?? "", sugars: p.sugars ?? "", protein: p.protein ?? "", fat: p.fat ?? "", calories: p.calories ?? "", barcode: code, source: "barcode" });
    } catch (err) {
      toast.error(err.message || "Couldn't look up that barcode.");
    }
    setBusy(false);
  }, []);

  async function onLabelPhoto(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    try {
      const image = await fileToImageString(file);
      const r = await base44.functions.invoke("food", { action: "scan_label", image });
      const l = r.data?.label;
      if (!l) throw new Error(r.data?.error || "Couldn't read the label");
      openConfirm({ name: l.name || "", brand: l.brand || "", serving_label: l.serving_label || "1 serving", carbs: l.carbs ?? "", fiber: l.fiber ?? "", sugars: l.sugars ?? "", protein: l.protein ?? "", fat: l.fat ?? "", calories: l.calories ?? "", source: "label" });
    } catch (err) {
      toast.error(err.message || "Couldn't read the label.");
    }
    setBusy(false);
  }

  async function logPreset(p) {
    const servings = servingsById[p.id] ?? p.default_servings ?? 1;
    setBusy(true);
    try {
      const r = await base44.functions.invoke("food", { action: "log", preset_id: p.id, servings });
      if (r.data?.error) throw new Error(r.data.error);
      toast.success(`Logged ${p.name} × ${servings}`);
      await load();
      onLogged?.();
    } catch (err) { toast.error(err.message || "Couldn't log that."); }
    setBusy(false);
  }

  async function saveDraft() {
    const carbs = parseFloat(draft.carbs);
    if (!draft.name.trim() || Number.isNaN(carbs)) { toast.error("A name and carbs per serving are needed."); return; }
    setBusy(true);
    try {
      const fields = { name: draft.name, brand: draft.brand, serving_label: draft.serving_label, carbs, fiber: draft.fiber || null, sugars: draft.sugars || null, protein: draft.protein || null, fat: draft.fat || null, calories: draft.calories || null };
      let presetId = draft.preset_id;
      if (draft.savePreset && !presetId) {
        const s = await base44.functions.invoke("food", { action: "save_preset", ...fields, barcode: draft.barcode, source: draft.source, default_servings: draft.servings });
        presetId = s.data?.preset?.id || null;
      }
      const r = await base44.functions.invoke("food", { action: "log", ...(presetId ? { preset_id: presetId } : fields), servings: draft.servings, timestamp: new Date(when).toISOString() });
      if (r.data?.error) throw new Error(r.data.error);
      toast.success(`Logged ${draft.name} × ${draft.servings}`);
      setMode(null);
      await load();
      onLogged?.();
    } catch (err) { toast.error(err.message || "Couldn't log that."); }
    setBusy(false);
  }

  async function deleteLog(id) {
    await base44.functions.invoke("food", { action: "delete_log", id });
    await load();
    onLogged?.();
  }

  const Stepper = ({ value, onChange }) => (
    <div className="inline-flex items-center rounded-md border border-border">
      <button type="button" className="px-2 py-1 text-sm" onClick={() => onChange(Math.max(0.25, +(value - 0.5).toFixed(2)))} aria-label="fewer servings"><Minus className="w-3 h-3" /></button>
      <span className="px-2 text-sm tabular-nums min-w-[2.5rem] text-center">{value}</span>
      <button type="button" className="px-2 py-1 text-sm" onClick={() => onChange(Math.min(20, +(value + 0.5).toFixed(2)))} aria-label="more servings"><Plus className="w-3 h-3" /></button>
    </div>
  );

  return (
    <div className="bg-card rounded-xl border border-border p-4 space-y-3">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h3 className="text-sm font-semibold flex items-center gap-2"><Utensils className="w-4 h-4 text-primary" /> Food <span className="text-xs font-normal text-muted-foreground">scan, snap, or tap</span></h3>
        <div className="flex gap-2">
          <Button size="sm" onClick={() => setMode("scan")} disabled={busy}><ScanBarcode className="w-4 h-4 mr-1" /> Scan barcode</Button>
          <Button size="sm" variant="outline" onClick={() => fileRef.current?.click()} disabled={busy}><Camera className="w-4 h-4 mr-1" /> Label photo</Button>
          <Button size="sm" variant="outline" onClick={() => openConfirm({})} disabled={busy}><Plus className="w-4 h-4 mr-1" /> Manual</Button>
          <input ref={fileRef} type="file" accept="image/*" capture="environment" className="hidden" onChange={onLabelPhoto} data-testid="label-photo-input" />
        </div>
      </div>
      {busy && mode !== "confirm" && <div className="text-xs text-muted-foreground flex items-center gap-2"><Loader2 className="w-3 h-3 animate-spin" /> working…</div>}

      {mode === "scan" && <BarcodeScanner onDetected={onBarcode} onClose={() => setMode(null)} />}

      {mode === "confirm" && (
        <div className="rounded-lg border border-border p-3 space-y-2 bg-muted/20">
          <div className="flex items-center justify-between">
            <div className="text-xs font-medium">{draft.source === "barcode" ? "From barcode" : draft.source === "label" ? "From label photo — check the numbers" : "Manual entry"}</div>
            <button type="button" onClick={() => setMode(null)} aria-label="cancel"><X className="w-4 h-4" /></button>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Input placeholder="Name" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} aria-label="food name" />
            <Input placeholder="Serving (e.g. 1 cup)" value={draft.serving_label} onChange={(e) => setDraft({ ...draft, serving_label: e.target.value })} aria-label="serving" />
          </div>
          <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
            {[["carbs", "Carbs g"], ["fiber", "Fiber g"], ["sugars", "Sugars g"], ["protein", "Protein g"], ["fat", "Fat g"], ["calories", "kcal"]].map(([k, label]) => (
              <Input key={k} type="number" inputMode="decimal" placeholder={label} value={draft[k]} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} aria-label={label} className={k === "carbs" ? "border-primary" : ""} />
            ))}
          </div>
          <div className="flex items-center gap-3 flex-wrap text-xs">
            <span>Servings</span><Stepper value={draft.servings} onChange={(v) => setDraft({ ...draft, servings: v })} />
            <Input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} className="w-auto text-xs" aria-label="when" />
            <label className="inline-flex items-center gap-1"><input type="checkbox" checked={draft.savePreset} onChange={(e) => setDraft({ ...draft, savePreset: e.target.checked })} /> Save as preset</label>
            <Button size="sm" onClick={saveDraft} disabled={busy}>{busy ? <Loader2 className="w-4 h-4 animate-spin" /> : "Log it"}</Button>
          </div>
          {draft.carbs !== "" && <div className="text-[11px] text-muted-foreground">Logs <b>{(parseFloat(draft.carbs || 0) * draft.servings).toFixed(1)} g carbs</b> total.</div>}
        </div>
      )}

      {presets.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1 flex items-center gap-1"><Star className="w-3 h-3" /> Presets</div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
            {presets.slice(0, 9).map((p) => (
              <div key={p.id} className="rounded-lg border border-border p-2 flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-sm font-medium truncate">{p.name}</div>
                  <div className="text-[11px] text-muted-foreground truncate">{p.carbs} g carbs · {p.serving_label}</div>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <Stepper value={servingsById[p.id] ?? p.default_servings ?? 1} onChange={(v) => setServingsById({ ...servingsById, [p.id]: v })} />
                  <Button size="sm" onClick={() => logPreset(p)} disabled={busy} aria-label={`log ${p.name}`}>Log</Button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {recent.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">Recent</div>
          <ul className="text-xs space-y-1">
            {recent.slice(0, 6).map((t) => (
              <li key={t.id} className="flex items-center justify-between gap-2">
                <span className="truncate">{new Date(t.timestamp).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} · {t.food_name} × {t.servings} · <b>{t.amount} g</b>{t.protein_g != null && <span className="text-muted-foreground"> · {t.protein_g} g protein</span>}</span>
                <button type="button" onClick={() => deleteLog(t.id)} aria-label={`delete ${t.food_name}`}><Trash2 className="w-3 h-3 text-muted-foreground" /></button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
