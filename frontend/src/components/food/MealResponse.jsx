import { useState } from "react";
import { base44 } from "@/api/base44Client";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";

const REASON_LABEL = {
  no_glucose: "no CGM at the meal",
  sparse_glucose: "CGM gaps after",
  temp_basal: "temp basal",
  bolus: "bolus",
  falling: "falling at the start",
  rising: "rising at the start",
};

const fmtTrend = (v) => (v == null ? "" : ` (${v > 0 ? "+" : ""}${v}/30m)`);
const fmtMin = (v) => (v == null ? "—" : `${v} min`);

function Flags({ meal }) {
  const flags = [];
  if (meal.temp_basal) flags.push("temp basal");
  if (meal.bolus) flags.push("bolus");
  if (meal.walking_minutes > 0) flags.push(`walked ${meal.walking_minutes}m`);
  if (meal.low_before) flags.push("low before");
  return flags.length ? <span className="text-muted-foreground">{flags.join(" · ")}</span> : null;
}

export default function MealResponse() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (!next || data || loading) return;
    setLoading(true);
    try {
      const r = await base44.functions.invoke("food", { action: "meal_response" });
      if (r.data?.error) throw new Error(r.data.error);
      setData(r.data);
      setError(null);
    } catch (err) { setError(err.message || "Couldn't load meal responses."); }
    setLoading(false);
  }

  const s = data?.summary;
  const reasons = Object.entries(data?.not_readable_reasons || {});
  return (
    <div className="border-t border-border pt-2">
      <button type="button" onClick={toggle} className="text-[10px] uppercase tracking-wide text-muted-foreground flex items-center gap-1" aria-expanded={open}>
        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />} After meals
      </button>
      {open && (
        <div className="mt-2 space-y-2 text-xs">
          {loading && <div className="text-muted-foreground flex items-center gap-2"><Loader2 className="w-3 h-3 animate-spin" /> measuring…</div>}
          {error && <div className="text-muted-foreground">{error}</div>}
          {data && (
            <>
              <p>
                {s?.rise_start_minutes != null
                  ? <>Across {s.n} readable meals, glucose started rising a median <b>{s.rise_start_minutes} min</b> after the logged time and peaked at <b>{fmtMin(s.peak_minutes)}</b>{s.rise != null && <>, {s.rise} mg/dL above its low point</>}.</>
                  : s ? <>{s.n} readable meals, {s.n_with_rise} with a clear rise; a median shows once {data.min_group} have one.</>
                  : <>A median shows once {data.min_group} meals are readable.</>}
              </p>
              <p className="text-muted-foreground">
                {data.meals_readable} of {data.meals_total} meals in the last 90 days readable.
                {reasons.length > 0 && <> Set aside: {reasons.map(([k, n]) => `${REASON_LABEL[k] || k} ${n}`).join(", ")}.</>}
              </p>
              {data.meals.length > 0 && (
                <div className="overflow-x-auto">
                  <table className="w-full">
                    <thead><tr className="text-muted-foreground text-left"><th className="py-1 pr-2">Meal</th><th className="pr-2">Start</th><th className="pr-2">Rise starts</th><th className="pr-2">Peak</th><th className="pr-2">Context</th></tr></thead>
                    <tbody>
                      {data.meals.slice(0, 10).map((m) => (
                        <tr key={m.id || m.timestamp} className={`border-t border-border ${m.readable ? "" : "text-muted-foreground"}`}>
                          <td className="py-1 pr-2 max-w-[10rem] truncate">{new Date(m.timestamp).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })} · {m.food_name || "Meal"}</td>
                          <td className="pr-2 tabular-nums whitespace-nowrap">{m.start_glucose ?? "—"}{fmtTrend(m.trend_30)}</td>
                          <td className="pr-2 tabular-nums">{fmtMin(m.rise_start_minutes)}</td>
                          <td className="pr-2 tabular-nums whitespace-nowrap">{m.peak != null ? `${m.peak} at ${m.peak_minutes} min` : "—"}</td>
                          <td className="pr-2">{m.readable ? <Flags meal={m} /> : <span>not readable: {m.reasons.map((r) => REASON_LABEL[r] || r).join(", ")}</span>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="text-[11px] text-muted-foreground">{data.semantics?.readable} {data.semantics?.association}</p>
            </>
          )}
        </div>
      )}
    </div>
  );
}
