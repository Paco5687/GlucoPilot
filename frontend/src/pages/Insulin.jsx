import { useState, useEffect, useCallback } from "react";
import { base44 } from "@/api/base44Client";
import SafetyBanner from "../components/SafetyBanner";
import DataQualityNote from "@/components/DataQualityNote";
import ContradictionPanel from "@/components/ContradictionPanel";
import ManagementBurdenCard from "@/components/insulin/ManagementBurdenCard";
import { Syringe, Loader2, TrendingUp, TrendingDown, AlertTriangle, ChevronRight } from "lucide-react";

const CAT = {
  low: { label: "Insulin-sensitive", cls: "text-blue-600", bg: "bg-blue-500/10" },
  typical: { label: "Typical", cls: "text-emerald-600", bg: "bg-emerald-500/10" },
  elevated: { label: "Somewhat resistant", cls: "text-amber-600", bg: "bg-amber-500/10" },
  high: { label: "More resistant", cls: "text-rose-600", bg: "bg-rose-500/10" },
  unknown: { label: "—", cls: "text-muted-foreground", bg: "bg-muted" },
};

const CONSISTENCY = {
  "highly variable": { cls: "text-rose-600", bg: "bg-rose-500/10" },
  variable: { cls: "text-amber-600", bg: "bg-amber-500/10" },
  consistent: { cls: "text-emerald-600", bg: "bg-emerald-500/10" },
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

function Section({ title, children }) {
  return (
    <section className="space-y-3">
      <h2 className="font-semibold text-base">{title}</h2>
      {children}
    </section>
  );
}

function daysAgo(iso) {
  if (!iso) return null;
  return Math.round((Date.now() - new Date(iso + "T12:00:00").getTime()) / 86400000);
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
  const consistency = CONSISTENCY[absn?.consistency] || {};
  const measured = absn?.available && absn?.counts?.total > 0;
  const strata = Object.entries(absn?.analysis?.strata || {}).filter(([, rows]) => rows.length > 0);

  if (loading) {
    return <div className="flex items-center justify-center h-40"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>;
  }

  return (
    <div className="space-y-6">
      <SafetyBanner />
      <div>
        <h1 className="text-xl font-bold flex items-center gap-2"><Syringe className="w-5 h-5 text-primary" /> Insulin</h1>
        <p className="text-sm text-muted-foreground mt-1">How much insulin you use, how it's trending, and what one unit does.</p>
      </div>

      <ContradictionPanel domains={["pump_tdd"]} title="Pump total contradictions" />

      {!r?.available ? (
        <div className="bg-card rounded-xl border border-border p-8 text-center text-sm text-muted-foreground">
          {r?.needs_weight
            ? "Add your weight in Settings → Body profile to unlock this page."
            : (r?.reason || "Not enough pump data yet — this page needs daily insulin totals from your pump.")}
        </div>
      ) : (
        <>
          {stale && (
            <div className="rounded-xl border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800 flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              <span>Showing the most recent complete pump data, through <b>{r.data_through}</b> ({daysAgo(r.data_through)} days ago) — not current dosing.</span>
            </div>
          )}

          <Section title="How much you use">
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
              <div className="bg-card rounded-xl border border-border p-5">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Daily insulin</div>
                <div className="text-3xl font-bold mt-1 tabular-nums">{r.avg_tdd} <span className="text-sm font-normal text-muted-foreground">U/day</span></div>
                <div className="text-sm text-muted-foreground mt-1">average over {r.n_days} days</div>
                {r.basal_pct != null && (
                  <div className="mt-3">
                    <div className="flex h-2 rounded-full overflow-hidden bg-muted">
                      <div className="bg-primary" style={{ width: `${r.basal_pct}%` }} />
                    </div>
                    <div className="text-[11px] text-muted-foreground mt-1">{r.basal_pct}% basal · {Math.round(100 - r.basal_pct)}% bolus</div>
                  </div>
                )}
              </div>
              {r.trend ? (
                <div className="bg-card rounded-xl border border-border p-5">
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Trend</div>
                  <div className="flex items-center gap-2 mt-1">
                    {r.trend.pct_change > 0 ? <TrendingUp className="w-5 h-5 text-rose-500" /> : <TrendingDown className="w-5 h-5 text-emerald-500" />}
                    <span className="text-3xl font-bold tabular-nums">{r.trend.pct_change > 0 ? "+" : ""}{r.trend.pct_change}%</span>
                  </div>
                  <div className="text-sm text-muted-foreground mt-1">
                    recently <b className="tabular-nums">{r.trend.recent_tdd} U</b>/day, was <b className="tabular-nums">{r.trend.prior_tdd} U</b>/day
                  </div>
                </div>
              ) : <div className="hidden lg:block" />}
              <div className={`rounded-xl border border-border p-5 ${cat.bg}`}>
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground">For your body weight</div>
                <div className={`text-3xl font-bold mt-1 ${cat.cls}`}>{cat.label}</div>
                <div className="text-sm text-muted-foreground mt-1">
                  <b className="tabular-nums">{r.tdd_per_kg}</b> U per kg per day{r.weight_kg && <> at {r.weight_kg} kg</>}
                </div>
                <div className="text-[11px] text-muted-foreground mt-2">Under 0.4 counts as sensitive; over 0.8 as resistant.</div>
              </div>
            </div>
          </Section>
        </>
      )}

      {(r?.available || absn) && (
        <Section title="What one unit does">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {r?.available && (
              <>
                <Stat label="Correction (estimated)" value={r.est_isf_mgdl_per_u} unit="mg/dL"
                  sub="1 unit should lower glucose about this much" />
                <Stat label="Carbs (estimated)" value={r.est_carb_ratio_g_per_u} unit="g"
                  sub="1 unit should cover about this many grams" />
              </>
            )}
            {measured && absn.available ? (
              <div className={`rounded-xl border border-border p-4 ${consistency.bg || "bg-card"}`}>
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Correction (measured)</div>
                <div className="text-xl font-semibold tabular-nums">{absn.median_drop_per_unit} <span className="text-xs text-muted-foreground">mg/dL</span></div>
                <div className="text-[11px] text-muted-foreground mt-0.5">
                  median drop across {absn.n} real correction doses — <span className={`capitalize font-medium ${consistency.cls || ""}`}>{absn.consistency}</span> (range {absn.min_drop_per_unit}–{absn.max_drop_per_unit})
                </div>
              </div>
            ) : (
              <Stat label="Correction (measured)" value={null}
                sub={absn?.reason || "No usable correction doses yet"} />
            )}
          </div>
          <p className="text-[11px] text-muted-foreground">
            Estimates use the standard 1800/500 rules from your daily totals; "measured" watches what glucose actually did after standalone correction doses. Neither is a pump setting — bring differences to your care team.
          </p>
        </Section>
      )}

      {(Object.keys(r?.per_phase_tdd_per_kg || {}).length > 0 || Object.keys(r?.mode_split?.modes || {}).length > 0 || strata.length > 0) && (
        <Section title="Patterns">
          {Object.keys(r?.per_phase_tdd_per_kg || {}).length > 0 && (
                <div className="bg-card rounded-xl border border-border p-4">
                  <h3 className="text-sm font-semibold mb-2">Insulin needs across the cycle</h3>
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                    {Object.entries(r.per_phase_tdd_per_kg).sort((a, b) => b[1] - a[1]).map(([ph, v]) => (
                      <div key={ph} className="text-sm">
                        <div className="text-xs text-muted-foreground capitalize">{ph}</div>
                        <div className="font-semibold tabular-nums">{v} <span className="text-[10px] text-muted-foreground">U/kg</span></div>
                      </div>
                    ))}
                  </div>
                  <p className="text-[11px] text-muted-foreground mt-2">Higher = more insulin needed in that phase. Needing more in the luteal phase is common in T1D.</p>
                </div>
              )}
              {Object.keys(r?.mode_split?.modes || {}).length > 0 && (
                <div className="bg-card rounded-xl border border-border p-4">
                  <h3 className="text-sm font-semibold mb-2">Time in range by pump mode <span className="font-normal text-muted-foreground">· last {r.mode_split.window_days} days</span></h3>
                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                    {Object.entries(r.mode_split.modes).map(([mode, m]) => (
                      <div key={mode} className="rounded-lg border border-border p-3 text-sm">
                        <div className="text-xs text-muted-foreground capitalize mb-1">{mode} mode · {m.pct_time}% of time</div>
                        <div className="font-semibold tabular-nums">
                          {m.tir_70_180 != null ? `${m.tir_70_180}% in range` : "no CGM overlap"}
                        </div>
                        {m.avg_glucose != null && (
                          <div className="text-xs text-muted-foreground tabular-nums">avg {m.avg_glucose} mg/dL · {m.n_readings.toLocaleString()} readings</div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {strata.length > 0 && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {strata.map(([dimension, rows]) => (
                    <div key={dimension} className="bg-card rounded-xl border border-border p-3">
                      <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-2">Measured correction drop by {dimension.replaceAll("_", " ")}</div>
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
        </Section>
      )}

      <ManagementBurdenCard days={90} />

      {/* Everything about method, coverage, and limits lives here — visible on
          demand instead of interleaved with the numbers it qualifies. */}
      <details className="rounded-xl border border-border bg-muted/20 group">
        <summary className="flex items-center gap-2 cursor-pointer select-none p-3 text-sm font-medium text-muted-foreground">
          <ChevronRight className="w-4 h-4 transition-transform group-open:rotate-90" />
          How these numbers are computed
        </summary>
        <div className="px-4 pb-4 space-y-3 text-xs text-muted-foreground">
          {r?.available && (
            <>
              <DataQualityNote label="Pump TDD" quality={r.quality} />
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
              {r.reconciliation?.discrepancy_days > 0 && (
                <p className="rounded-lg border border-amber-300 bg-amber-50 p-2 text-amber-800">
                  {r.reconciliation.discrepancy_days} day{r.reconciliation.discrepancy_days === 1 ? "" : "s"} differed by more than rounding between pump-reported and calculated TDD. The values are kept separate rather than silently combined.
                </p>
              )}
              {r.reconciliation?.limitations?.map((limitation) => <p key={limitation}>{limitation}</p>)}
              <p>
                The weight-based category is a screening proxy that assumes complete daily insulin and a current body weight. It does not diagnose biologic insulin resistance or distinguish absorption, meals, illness, stress, activity, or dosing strategy. The pump-mode comparison is an observation to explore with the care team, not a settings verdict.
              </p>
            </>
          )}
          {absn && (
            <>
              <DataQualityNote label="Insulin response" quality={absn.quality} />
              {measured ? (
                <>
                  <p>
                    Measured correction response: of {absn.counts.total} candidate windows in the last {absn.window_days} days, {absn.counts.clean} were clean enough to use; {absn.counts.confounded} were confounded (kept, but excluded from summaries) and {absn.counts.excluded} were excluded outright.
                    {absn.available && <> Mean drop {absn.mean_drop_per_unit} mg/dL/U · variability (CV) {absn.cv_pct}% · {absn.confidence?.discovery_status || "not assessed"} · {absn.confidence?.confidence_label || "low"} confidence.</>}
                  </p>
                  {Object.keys(absn.reason_counts || {}).length > 0 && (
                    <p>
                      Exclusion and confounder reasons: {Object.entries(absn.reason_counts).map(([reason, count]) => `${reason.replaceAll("_", " ")} (${count})`).join(" · ")}.
                    </p>
                  )}
                  <p>
                    Each event records a {absn.response_window_minutes}-minute glucose window under algorithm {absn.algorithm_version}. The observed glucose change does not establish insulin causation, resistance, or absorption. Estimated IOB is a comparison assumption, not pump-reported IOB, and does not model basal insulin, insulin type, personal action curves, or dose absorption.
                  </p>
                </>
              ) : (
                <p>Measured correction response: {absn.reason}</p>
              )}
            </>
          )}
        </div>
      </details>
    </div>
  );
}
