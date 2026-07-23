import { useState, useEffect, useCallback } from "react";
import { base44 } from "@/api/base44Client";
import SafetyBanner from "../components/SafetyBanner";
import DataQualityNote from "@/components/DataQualityNote";
import ContradictionPanel from "@/components/ContradictionPanel";
import { Syringe, Loader2, TrendingUp, TrendingDown, AlertTriangle } from "lucide-react";

const CAT = {
  low: { label: "Insulin-sensitive", cls: "text-blue-600", bg: "bg-blue-500/10" },
  typical: { label: "Typical", cls: "text-emerald-600", bg: "bg-emerald-500/10" },
  elevated: { label: "Somewhat resistant", cls: "text-amber-600", bg: "bg-amber-500/10" },
  high: { label: "More resistant", cls: "text-rose-600", bg: "bg-rose-500/10" },
  unknown: { label: "—", cls: "text-muted-foreground", bg: "bg-muted" },
};

function Stat({ label, value, unit = null, sub = null }) {
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

const CONSISTENCY = {
  "highly variable": { cls: "text-rose-600", bg: "bg-rose-500/10" },
  variable: { cls: "text-amber-600", bg: "bg-amber-500/10" },
  consistent: { cls: "text-emerald-600", bg: "bg-emerald-500/10" },
};

function ResponseEvents({ absn }) {
  if (!absn) return null;
  if (!absn.counts?.total) {
    return (
      <div className="space-y-2">
        <p className="text-xs text-muted-foreground">Insulin response events: {absn.reason}</p>
        <DataQualityNote label="Insulin response" quality={absn.quality} />
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <h2 className="font-semibold text-base">Observed insulin response events</h2>
      <DataQualityNote label="Insulin response" quality={absn.quality} />
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat label="Response windows" value={absn.counts.total} sub={`${absn.window_days}-day source window`} />
        <Stat label="Clean by default" value={absn.counts.clean} sub="used in summaries" />
        <Stat label="Confounded" value={absn.counts.confounded} sub="retained, not summarized" />
        <Stat label="Excluded" value={absn.counts.excluded} sub="invalid or insufficient input" />
      </div>
      {absn.available ? (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className={`rounded-xl border border-border p-5 ${(CONSISTENCY[absn.consistency] || {}).bg}`}>
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Observed response variability</div>
            <div className={`text-2xl font-bold mt-1 capitalize ${(CONSISTENCY[absn.consistency] || {}).cls}`}>{absn.consistency}</div>
            <div className="text-sm text-muted-foreground mt-1">variability (CV) <b className="tabular-nums">{absn.cv_pct}%</b></div>
            <div className="text-[11px] text-muted-foreground mt-2">
              Across {absn.n} clean response windows · {absn.confidence?.discovery_status || "not assessed"} · {absn.confidence?.confidence_label || "low"} confidence.
            </div>
          </div>
          <div className="lg:col-span-2 grid grid-cols-2 sm:grid-cols-4 gap-3">
            <Stat label="Median observed drop" value={absn.median_drop_per_unit} unit="mg/dL/U" />
            <Stat label="Mean observed drop" value={absn.mean_drop_per_unit} unit="mg/dL/U" />
            <Stat label="Observed range" value={`${absn.min_drop_per_unit}–${absn.max_drop_per_unit}`} sub="mg/dL per unit" />
            <Stat label="Expected (ISF)" value={absn.expected_isf} unit="mg/dL/U" sub="1800 rule" />
          </div>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">{absn.reason}</p>
      )}
      {Object.keys(absn.reason_counts || {}).length > 0 && (
        <div className="bg-muted/40 rounded-xl border border-border p-3">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">Explicit exclusion and confounder reasons</div>
          <div className="text-xs text-muted-foreground">
            {Object.entries(absn.reason_counts).map(([reason, count]) => `${reason.replaceAll("_", " ")} (${count})`).join(" · ")}
          </div>
        </div>
      )}
      {absn.available && Object.entries(absn.analysis?.strata || {}).some(([, rows]) => rows.length > 0) && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {Object.entries(absn.analysis.strata).map(([dimension, rows]) => rows.length > 0 && (
            <div key={dimension} className="bg-card rounded-xl border border-border p-3">
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-2">{dimension.replaceAll("_", " ")}</div>
              <div className="space-y-1">
                {rows.map((row) => (
                  <div key={row.value} className="flex justify-between gap-3 text-xs">
                    <span className="capitalize">{row.value}</span>
                    <span className="tabular-nums text-muted-foreground">{row.median_nadir_drop_per_unit_mg_dl} mg/dL/U · n={row.sample_count}</span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
      <p className="text-[11px] text-muted-foreground">
        Each event records a {absn.response_window_minutes}-minute glucose window under algorithm {absn.algorithm_version}. Confounded events remain visible but are excluded from summaries by default. The observed glucose change does not establish insulin causation, resistance, or absorption. Estimated IOB is a comparison assumption, not pump-reported IOB, and does not model basal insulin, insulin type, personal action curves, or dose absorption.
      </p>
    </div>
  );
}

export default function Insulin() {
  const [r, setR] = useState(null);
  const [absn, setAbsn] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [res, ab] = await Promise.all([
        base44.functions.invoke("insulin", { action: "resistance" }),
        base44.functions.invoke("insulin", { action: "absorption", include_events: false }),
      ]);
      setR(res.data);
      setAbsn(ab.data);
    } catch { setR(null); setAbsn(null); }
    setLoading(false);
  }, []);
  useEffect(() => { load(); }, [load]);

  const stale = r?.available && r.current === false;
  const cat = CAT[r?.category] || CAT.unknown;

  return (
    <div className="space-y-6">
      <SafetyBanner />
      <div>
        <h1 className="text-xl font-bold flex items-center gap-2"><Syringe className="w-5 h-5 text-primary" /> Insulin</h1>
        <p className="text-sm text-muted-foreground mt-1">Resistance / sensitivity proxies from your dosing, glucose, cycle, and body profile. Observational estimates, not diagnoses or clinical settings.</p>
      </div>

      <ContradictionPanel domains={["pump_tdd"]} title="Pump total contradictions" />

      {loading ? (
        <div className="flex items-center justify-center h-40"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>
      ) : !r?.available ? (
        <div className="bg-card rounded-xl border border-border p-8 text-center text-sm text-muted-foreground space-y-2">
          <p>{r?.needs_weight ? "Add your weight in Settings → Body profile to compute TDD/kg." : (r?.reason || "Not enough pump data yet — needs Daily Total (basal + bolus) records.")}</p>
          {r?.latest_insulin_activity && <p className="text-xs">Latest insulin activity: {r.latest_insulin_activity}. It is not labeled as complete TDD.</p>}
          {r?.reconciliation?.limitations?.map((limitation) => <p key={limitation} className="text-xs">{limitation}</p>)}
          <DataQualityNote label="Pump TDD" quality={r?.quality} />
        </div>
      ) : (
        <>
          {stale && (
            <div className="rounded-xl border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800 flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              <span>Based on the most recent <b>complete pump data through {r.data_through}</b> ({daysAgo(r.data_through)} days ago). Newer insulin activity may lack a complete delivered-basal record, so this describes an earlier period—not current dosing.</span>
            </div>
          )}
          <DataQualityNote label="Pump TDD" quality={r.quality} />

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
              <Stat label="Selected complete TDD" value={r.avg_tdd} unit="U/day" sub={`over ${r.n_days} days`} />
              <Stat label="Basal share" value={r.basal_pct} unit="%" sub="of total insulin" />
              <Stat label="Est. ISF" value={r.est_isf_mgdl_per_u} unit="mg/dL/U" sub="1800 rule" />
              <Stat label="Est. carb ratio" value={r.est_carb_ratio_g_per_u} unit="g/U" sub="500 rule" />
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <Stat
              label="Pump-reported TDD"
              value={r.reconciliation?.pump_reported_avg_tdd}
              unit="U/day"
              sub={`${r.reconciliation?.pump_reported_days || 0} complete day${r.reconciliation?.pump_reported_days === 1 ? "" : "s"} · ${Object.keys(r.reconciliation?.pump_reported_sources || {}).join(", ") || "no source"}`}
            />
            <Stat
              label="Calculated TDD"
              value={r.reconciliation?.calculated_avg_tdd}
              unit="U/day"
              sub={`${r.reconciliation?.calculated_days || 0} full delivered-basal day${r.reconciliation?.calculated_days === 1 ? "" : "s"}`}
            />
          </div>
          <p className="text-[11px] text-muted-foreground">
            The TDD/kg category is a screening proxy that assumes complete daily insulin and a current body weight. It does not diagnose biologic insulin resistance or distinguish absorption, meals, illness, stress, activity, or dosing strategy.
          </p>

          {r.reconciliation?.limitations?.length > 0 && (
            <div className="bg-muted/40 rounded-xl border border-border p-3 text-xs text-muted-foreground space-y-1">
              {r.reconciliation.limitations.map((limitation) => <p key={limitation}>{limitation}</p>)}
            </div>
          )}

          {r.reconciliation?.discrepancy_days > 0 && (
            <div className="rounded-xl border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800">
              {r.reconciliation.discrepancy_days} day{r.reconciliation.discrepancy_days === 1 ? "" : "s"} differed by more than rounding between pump-reported and calculated TDD. The values are retained separately rather than silently combined.
            </div>
          )}

          {r.trend && (
            <div className="bg-card rounded-xl border border-border p-4 flex items-center gap-3">
              {r.trend.pct_change > 0 ? <TrendingUp className="w-5 h-5 text-rose-500" /> : <TrendingDown className="w-5 h-5 text-emerald-500" />}
              <div className="text-sm">
                <span className="font-medium">TDD trend:</span> {r.trend.recent_tdd} U recently vs {r.trend.prior_tdd} U earlier
                <span className="text-muted-foreground"> ({r.trend.pct_change > 0 ? "+" : ""}{r.trend.pct_change}%)</span>
                {Math.abs(r.trend.pct_change) >= 10 && <span className="text-muted-foreground"> — an observed dosing change; resistance, absorption, illness, meals, and activity are not distinguishable from TDD alone</span>}
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
      {!loading && <ResponseEvents absn={absn} />}
    </div>
  );
}
