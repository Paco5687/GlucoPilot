import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Loader2, Sparkles, Star, X } from "lucide-react";

export const MEAL_MACROS = [["carbs", "Carbs g"], ["fiber", "Fiber g"], ["sugars", "Sugars g"], ["protein", "Protein g"], ["fat", "Fat g"], ["calories", "kcal"]];

function num(v) {
  if (v === "" || v == null) return null;
  const n = parseFloat(v);
  return Number.isNaN(n) ? null : n;
}

const squash = (text) => (text || "").split(/\s+/).filter(Boolean).join(" ").toLowerCase();

export function mealTotals(items) {
  const totals = {};
  for (const [k] of MEAL_MACROS) {
    const values = items.map((i) => num(i[k])).filter((v) => v != null);
    totals[k] = values.length ? Math.round(values.reduce((a, b) => a + b, 0) * 10) / 10 : null;
  }
  return totals;
}

export function itemsForLog(items) {
  return items.map((i) => ({ name: i.name, portion: i.portion || null, preset_id: i.preset_id || null, servings: i.servings ?? null, ...Object.fromEntries(MEAL_MACROS.map(([k]) => [k, num(i[k])])) }));
}

// Confirm step for a described meal: what was heard (editable, re-estimable),
// one editable row per food, and the totals that get logged.
export default function VoiceMealPanel({ meal, setMeal, when, setWhen, busy, onEstimate, onLog, onCancel }) {
  const items = meal.items;
  const totals = mealTotals(items);
  const canEstimate = !busy && !!meal.transcript.trim();
  // The words were changed after estimating: the foods below no longer match them.
  const stale = items.length > 0 && squash(meal.transcript) !== squash(meal.estimatedFrom);
  const setItem = (idx, patch) => setMeal({ ...meal, items: items.map((it, i) => (i === idx ? { ...it, ...patch } : it)) });
  const removeItem = (idx) => setMeal({ ...meal, items: items.filter((_, i) => i !== idx) });

  return (
    <div className="rounded-lg border border-border p-3 space-y-2 bg-muted/20">
      <div className="flex items-center justify-between">
        <div className="text-xs font-medium">{items.length ? "From what you said — estimates, check the numbers" : "What did you eat?"}</div>
        <button type="button" onClick={onCancel} aria-label="cancel"><X className="w-4 h-4" /></button>
      </div>
      <form className="flex flex-col sm:flex-row gap-2" onSubmit={(e) => { e.preventDefault(); if (canEstimate) onEstimate(); }}>
        <Textarea rows={2} placeholder="e.g. chicken sandwich on gluten-free bread with Swiss" value={meal.transcript} onChange={(e) => setMeal({ ...meal, transcript: e.target.value })}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (canEstimate) onEstimate(); } }} aria-label="what you ate" className="min-h-0 resize-none" />
        <Button type="submit" size="sm" variant={items.length ? "outline" : "default"} disabled={!canEstimate} className="self-end sm:self-start">
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Sparkles className="w-4 h-4 mr-1" />{items.length ? "Re-estimate" : "Estimate"}</>}
        </Button>
      </form>

      {items.map((item, idx) => (
        <div key={idx} className="rounded-md border border-border bg-card p-2 space-y-1">
          <div className="flex items-center gap-2">
            {item.preset_id && <Star className="w-3 h-3 text-primary shrink-0" aria-label="saved food" />}
            <Input value={item.name} onChange={(e) => setItem(idx, { name: e.target.value })} aria-label={`food ${idx + 1} name`} className="h-8 flex-[3] min-w-0" />
            <Input value={item.portion || ""} placeholder="portion" onChange={(e) => setItem(idx, { portion: e.target.value })} aria-label={`food ${idx + 1} portion`} className="h-8 flex-[2] min-w-0" />
            <button type="button" onClick={() => removeItem(idx)} aria-label={`remove ${item.name}`}><X className="w-3 h-3 text-muted-foreground" /></button>
          </div>
          <div className="grid grid-cols-3 sm:grid-cols-6 gap-1">
            {MEAL_MACROS.map(([k, label]) => (
              <label key={k} className="text-[10px] text-muted-foreground">
                {label}
                <Input type="number" inputMode="decimal" min="0" value={item[k] ?? ""} onChange={(e) => setItem(idx, { [k]: e.target.value })} aria-label={`${label} for ${item.name}`} className={`h-8 px-2 ${k === "carbs" ? "border-primary" : ""}`} />
              </label>
            ))}
          </div>
        </div>
      ))}

      {items.length > 0 && (
        <>
          <div className="text-[11px]">
            Total <b>{totals.carbs ?? 0} g carbs</b>
            {totals.fiber != null && <> · {totals.fiber} g fiber</>}
            {totals.protein != null && <> · {totals.protein} g protein</>}
            {totals.fat != null && <> · {totals.fat} g fat</>}
            {totals.calories != null && <> · {Math.round(totals.calories)} kcal</>}
            {meal.confidence != null && meal.confidence < 0.5 && <div className="text-muted-foreground">Rough estimate — change anything you know better.</div>}
          </div>
          {stale && (
            <div role="alert" className="rounded-md border border-amber-500/50 bg-amber-500/10 p-2 text-xs space-y-1">
              <div>You changed what you ate after the estimate. Re-estimate so the foods match, or log the foods as listed.</div>
              <div className="flex gap-2">
                <Button size="sm" onClick={onEstimate} disabled={!canEstimate}><Sparkles className="w-4 h-4 mr-1" />Re-estimate</Button>
                <Button size="sm" variant="outline" onClick={onLog} disabled={busy}>Log foods as listed</Button>
              </div>
            </div>
          )}
          <div className="flex items-center gap-3 flex-wrap text-xs">
            <Input placeholder="Meal name" value={meal.title} onChange={(e) => setMeal({ ...meal, title: e.target.value })} aria-label="meal name" className="w-auto flex-1 min-w-[10rem] text-xs" />
            <Input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} className="w-auto text-xs" aria-label="when" />
            <label className="inline-flex items-center gap-1"><input type="checkbox" checked={meal.savePreset} onChange={(e) => setMeal({ ...meal, savePreset: e.target.checked })} /> Save as preset</label>
            <Button size="sm" onClick={onLog} disabled={busy || stale}>{busy ? <Loader2 className="w-4 h-4 animate-spin" /> : "Log it"}</Button>
          </div>
        </>
      )}
    </div>
  );
}
