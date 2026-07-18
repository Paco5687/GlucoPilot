import { useState, useEffect, useCallback } from "react";
import { base44 } from "@/api/base44Client";
import SafetyBanner from "../components/SafetyBanner";
import { Syringe, Loader2, TrendingUp, TrendingDown, AlertTriangle } from "lucide-react";

const CAT = {
  low: { label: "Insulin-sensitive", cls: "text-blue-600", bg: "bg-blue-500/10" },
  typical: { label: "Typical", cls: "text-emerald-600", bg: "bg-emerald-500/10" },
  elevated: { label: "Somewhat resistant", cls: "text-amber-600", bg: "bg-amber-500/10" },
  high: { label: "More resistant", cls: "text-rose-600", bg: "bg-rose-500/10" },
  unknown: { label: "—", cls: "text-muted-foreground", bg: "bg-muted" },
};

function Stat({ label, value, unit, sub }) {
  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="text-xl font-semibold tabular-nums">
        {value ?? "—"}{value != null && unit && <span className="text-xs text-muted-foreground ml-0.5">{unit}</span>}
      </div>
      {sub && <div className="text-[11px] text-muted-foreground mt-0.5">{sub}</div>}
    </div>
  );
}

function daysAgo(iso) {
  if (!iso) return null;
  return Math.round((Date.now() - new Date(iso + "T12:00:00").getTime()) / 86400000);
}

export default function Insulin() {
  const [r, setR] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await base44.functions.invoke("insulin", { action: "resistance" });
      setR(res.data);
    } catch { setR(null); }
    setLoading(false);
  }, []);
  useEffect(() => { load(); }, [load]);

  const stale = r?.data_through && daysAgo(r.data_through) > 14;
  const cat = CAT[r?.category] || CAT.unknown;

  return (
    <div className="space-y-6">
      <SafetyBanner />
      <div>
        <h1 className="text-xl font-bold flex items-center gap-2"><Syringe className="w-5 h-5 text-primary" /> Insulin</h1>
        <p className="text-sm text-muted-foreground mt-1">Resistance / sensitivity estimates from your dosing, glucose, cycle, and body profile. Estimates, not clinical settings.</p>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-40"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>
      ) : !r?.available ? (
        <div className="bg-card rounded-xl border border-border p-8 text-center text-sm text-muted-foreground">
          {r?.needs_weight ? "Add your weight in Settings → Body profile to compute TDD/kg." : (r?.reason || "Not enough pump data yet — needs Daily Total (basal + bolus) records.")}
        </div>
      ) : (
        <>
          {stale && (
            <div className="rounded-xl border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800 flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              <span>Based on your most recent <b>complete pump data through {r.data_through}</b> ({daysAgo(r.data_through)} days ago). Recent months only have bolus data (no basal), so this reflects that earlier period — not necessarily today.</span>
            </div>
          )}

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <div className={`rounded-xl border border-border p-5 lg:col-span-1 ${cat.bg}`}>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Insulin resistance estimate</div>
              <div className={`text-3xl font-bold mt-1 ${cat.cls}`}>{cat.label}</div>
              <div className="text-sm text-muted-foreground mt-1">
                TDD/kg <b className="tabular-nums">{r.tdd_per_kg}</b> U/kg/day
                {r.weight_kg && <> · {r.weight_kg} kg</>}
              </div>
              <div className="text-[11px] text-muted-foreground mt-2">Rule of thumb: &lt;0.4 sensitive · 0.4–0.6 typical · 0.6–0.8 elevated · &gt;0.8 resistant.</div>
            </div>
            <div className="lg:col-span-2 grid grid-cols-2 sm:grid-cols-4 gap-3">
              <Stat label="Avg TDD" value={r.avg_tdd} unit="U/day" sub={`over ${r.n_days} days`} />
              <Stat label="Basal share" value={r.basal_pct} unit="%" sub="of total insulin" />
              <Stat label="Est. ISF" value={r.est_isf_mgdl_per_u} unit="mg/dL/U" sub="1800 rule" />
              <Stat label="Est. carb ratio" value={r.est_carb_ratio_g_per_u} unit="g/U" sub="500 rule" />
            </div>
          </div>

          {r.trend && (
            <div className="bg-card rounded-xl border border-border p-4 flex items-center gap-3">
              {r.trend.pct_change > 0 ? <TrendingUp className="w-5 h-5 text-rose-500" /> : <TrendingDown className="w-5 h-5 text-emerald-500" />}
              <div className="text-sm">
                <span className="font-medium">TDD trend:</span> {r.trend.recent_tdd} U recently vs {r.trend.prior_tdd} U earlier
                <span className="text-muted-foreground"> ({r.trend.pct_change > 0 ? "+" : ""}{r.trend.pct_change}%)</span>
                {Math.abs(r.trend.pct_change) >= 10 && <span className="text-muted-foreground"> — {r.trend.pct_change > 0 ? "rising insulin need suggests increasing resistance" : "falling insulin need suggests improving sensitivity"}</span>}
              </div>
            </div>
          )}

          {Object.keys(r.per_phase_tdd_per_kg || {}).length > 0 && (
            <div className="bg-card rounded-xl border border-border p-4">
              <h3 className="text-sm font-semibold mb-2">TDD/kg by cycle phase</h3>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                {Object.entries(r.per_phase_tdd_per_kg).sort((a, b) => b[1] - a[1]).map(([ph, v]) => (
                  <div key={ph} className="text-sm">
                    <div className="text-xs text-muted-foreground capitalize">{ph}</div>
                    <div className="font-semibold tabular-nums">{v} <span className="text-[10px] text-muted-foreground">U/kg</span></div>
                  </div>
                ))}
              </div>
              <p className="text-[11px] text-muted-foreground mt-2">Higher = more insulin needed per kg in that phase (luteal-phase resistance is common in T1D).</p>
            </div>
          )}
        </>
      )}
    </div>
  );
}
