import { useState, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import DataQualityNote from "@/components/DataQualityNote";
import { Loader2, Printer, FileText, RefreshCw, TrendingUp, TrendingDown, Minus, AlertTriangle, ShieldCheck, Stethoscope, ScrollText } from "lucide-react";
import {
  ResponsiveContainer, AreaChart, Area, Line, XAxis, YAxis, Tooltip, ReferenceLine, CartesianGrid,
} from "recharts";

// Clinical AGP color convention (not brand palette) — this is a medical report.
const TIR_BANDS = [
  { key: "tbr54", label: "Very low (<54)", color: "#b91c1c" },
  { key: "tbr70", label: "Low (54–69)", color: "#ef4444" },
  { key: "tir", label: "In range (70–180)", color: "#16a34a" },
  { key: "tar180", label: "High (181–250)", color: "#f59e0b" },
  { key: "tar250", label: "Very high (>250)", color: "#d97706" },
];

const PRINT_CSS = `
@media print {
  header, .print\\:hidden { display: none !important; }
  main { padding: 0 !important; max-width: 100% !important; }
  .report-card { break-inside: avoid; box-shadow: none !important; border-color: #ddd !important; }
  .report-section { break-inside: avoid; }
  body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  @page { margin: 1.5cm; }
}
`;

function Metric({ label, value, sub = null, tone = "" }) {
  return (
    <div className="report-card bg-card rounded-lg border border-border p-3">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className={`text-2xl font-bold tabular-nums ${tone || ""}`}>{value}</div>
      {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

function TIRBar({ g }) {
  // Non-overlapping band widths from cumulative thresholds.
  const segs = [
    { label: "Very low (<54)", pct: g.tbr54, color: "#b91c1c" },
    { label: "Low (54–69)", pct: Math.max(0, g.tbr70 - g.tbr54), color: "#ef4444" },
    { label: "In range (70–180)", pct: g.tir, color: "#16a34a" },
    { label: "High (181–250)", pct: Math.max(0, g.tar180 - g.tar250), color: "#f59e0b" },
    { label: "Very high (>250)", pct: g.tar250, color: "#d97706" },
  ];
  return (
    <div>
      <div className="flex h-8 rounded-md overflow-hidden border border-border">
        {segs.map((s, i) => (
          s.pct > 0 ? (
            <div key={i} style={{ width: `${s.pct}%`, background: s.color }} title={`${s.label}: ${s.pct.toFixed(1)}%`} />
          ) : null
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-[11px]">
        {segs.map((s, i) => (
          <span key={i} className="flex items-center gap-1">
            <i className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: s.color }} />
            {s.label}: <b>{s.pct.toFixed(1)}%</b>
          </span>
        ))}
      </div>
    </div>
  );
}

function AGPChart({ agp }) {
  const data = agp
    .filter((h) => h.p50 != null)
    .map((h) => ({
      hour: h.hour,
      band: [h.p5, h.p95],
      iqr: [h.p25, h.p75],
      p50: h.p50,
    }));
  return (
    <div className="h-56">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" vertical={false} />
          <defs>
            <linearGradient id="agpOuter" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#2563eb" stopOpacity={0.12} />
              <stop offset="100%" stopColor="#2563eb" stopOpacity={0.12} />
            </linearGradient>
          </defs>
          <XAxis dataKey="hour" tickFormatter={(h) => `${h}:00`} tick={{ fontSize: 10 }} interval={2} />
          <YAxis domain={[40, 300]} ticks={[54, 70, 180, 250]} tick={{ fontSize: 10 }} width={32} />
          <Tooltip
            formatter={(v, n) => [Array.isArray(v) ? `${v[0]}–${v[1]} mg/dL` : `${v} mg/dL`, n === "p50" ? "median" : n === "band" ? "5–95%" : "25–75%"]}
            labelFormatter={(h) => `${h}:00`}
            contentStyle={{ fontSize: 12, borderRadius: 8 }}
          />
          <ReferenceLine y={70} stroke="#16a34a" strokeDasharray="4 4" strokeOpacity={0.6} />
          <ReferenceLine y={180} stroke="#16a34a" strokeDasharray="4 4" strokeOpacity={0.6} />
          <Area dataKey="band" stroke="none" fill="#2563eb" fillOpacity={0.1} />
          <Area dataKey="iqr" stroke="none" fill="#2563eb" fillOpacity={0.22} />
          <Line dataKey="p50" stroke="#1d4ed8" strokeWidth={2} dot={false} type="monotone" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

const TREND_ICON = { up: TrendingUp, down: TrendingDown, flat: Minus };

function importDate(time) {
  const value = time?.canonical_at;
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleDateString();
}

export default function Report() {
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(90);

  const generate = useCallback(async (d) => {
    setLoading(true);
    try {
      const res = await fetch("/api/report/visit", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ days: d }),
      });
      setReport(res.ok ? await res.json() : null);
    } catch {
      setReport(null);
    }
    setLoading(false);
  }, []);

  useEffect(() => { generate(days); }, [days, generate]);

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-3 text-muted-foreground">
        <Loader2 className="w-6 h-6 animate-spin text-primary" />
        <p className="text-sm">Compiling the report and writing the summary…</p>
      </div>
    );
  }
  if (!report?.glucose) return <p className="text-sm text-muted-foreground">Could not generate the report.</p>;

  const g = report.glucose;
  const i = report.insulin;
  const c = report.cycle;
  const w = report.wellness;
  const labs = report.labs;
  const n = report.narrative;

  return (
    <div className="space-y-5 max-w-4xl mx-auto">
      <style>{PRINT_CSS}</style>

      {/* Controls (hidden in print) */}
      <div className="flex items-center justify-between flex-wrap gap-3 print:hidden">
        <div className="flex items-center gap-2">
          {[30, 90, 180].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                days === d ? "bg-primary text-primary-foreground border-primary" : "bg-secondary border-border hover:bg-accent"
              }`}
            >
              {d} days
            </button>
          ))}
          <Button variant="outline" size="sm" onClick={() => generate(days)} className="gap-2">
            <RefreshCw className="w-3.5 h-3.5" /> Regenerate
          </Button>
        </div>
        <Button onClick={() => window.print()} className="gap-2">
          <Printer className="w-4 h-4" /> Print / Save PDF
        </Button>
      </div>

      {/* Report header */}
      <div className="report-section flex items-center justify-between border-b border-border pb-4">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <FileText className="w-6 h-6 text-primary" /> Health Summary
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            {report.days}-day review · {report.start_date} to {report.end_date}
          </p>
        </div>
        <div className="text-right text-xs text-muted-foreground">
          <div className="w-9 h-9 rounded-lg bg-primary flex items-center justify-center ml-auto mb-1">
            <span className="text-primary-foreground font-mono font-bold text-sm">GP</span>
          </div>
          Generated {new Date(report.generated_at).toLocaleDateString()}
        </div>
      </div>

      {/* Diagnosed conditions */}
      {report.conditions?.length > 0 && (
        <div className="report-section report-card rounded-xl border border-border p-4">
          <h2 className="font-semibold text-sm mb-2 flex items-center gap-2">
            <Stethoscope className="w-4 h-4 text-primary" /> Conditions
          </h2>
          <div className="flex flex-wrap gap-2">
            {report.conditions.map((c, i) => (
              <span key={i} className="text-xs px-2 py-1 rounded-full bg-muted">
                {c.name}
                {c.status && c.status !== "active" ? ` (${c.status})` : ""}
                {c.diagnosed ? ` · dx ${c.diagnosed}` : ""}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Health history */}
      {(report.history?.narrative || report.history?.events?.length > 0) && (
        <div className="report-section report-card rounded-xl border border-border p-4">
          <h2 className="font-semibold text-sm mb-2 flex items-center gap-2">
            <ScrollText className="w-4 h-4 text-primary" /> Health history
          </h2>
          {report.history.narrative && (
            <p className="text-sm leading-relaxed whitespace-pre-wrap mb-3">{report.history.narrative}</p>
          )}
          {report.history.events?.length > 0 && (
            <ul className="text-sm space-y-1">
              {report.history.events.map((e, i) => (
                <li key={i} className="flex flex-wrap gap-x-2">
                  <span className="text-muted-foreground tabular-nums whitespace-nowrap">{e.date || "—"}</span>
                  <span className="text-muted-foreground capitalize">· {e.type}</span>
                  <span className="font-medium">{e.title}</span>
                  {e.details && <span className="text-muted-foreground">— {e.details}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Medications & allergies */}
      {(report.medications?.length > 0 || report.allergies?.length > 0) && (
        <div className="report-section grid grid-cols-1 sm:grid-cols-2 gap-4">
          {report.medications?.length > 0 && (
            <div className="report-card rounded-xl border border-border p-4">
              <h2 className="font-semibold text-sm mb-2">Medications &amp; supplements</h2>
              <ul className="text-sm space-y-1">
                {report.medications.map((m, i) => (
                  <li key={i}>
                    {m.name}{m.dose ? ` ${m.dose}` : ""}{m.frequency ? ` · ${m.frequency}` : ""}
                    {m.kind === "supplement" ? " (supplement)" : ""}{m.status === "stopped" ? " — stopped" : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {report.allergies?.length > 0 && (
            <div className="report-card rounded-xl border border-border p-4">
              <h2 className="font-semibold text-sm mb-2">Allergies</h2>
              <ul className="text-sm space-y-1">
                {report.allergies.map((a, i) => (
                  <li key={i}>{a.allergen}{a.severity ? ` (${a.severity})` : ""}{a.reaction ? ` — ${a.reaction}` : ""}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* Insurance (prints at the top for the front desk) */}
      {report.insurance?.available && (
        <div className="report-section report-card rounded-xl border border-border p-4">
          <h2 className="font-semibold text-sm mb-2 flex items-center gap-2">
            <ShieldCheck className="w-4 h-4 text-primary" /> Health Insurance
          </h2>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-6 gap-y-1.5 text-sm">
            {[
              ["Carrier", report.insurance.carrier],
              ["Plan", [report.insurance.plan_name, report.insurance.plan_type].filter(Boolean).join(" · ")],
              ["Member", report.insurance.member_name],
              ["Member ID", report.insurance.member_id],
              ["Group", report.insurance.group_number],
              ["Member services", report.insurance.customer_service_phone],
              ["RxBIN", report.insurance.rx_bin],
              ["RxPCN", report.insurance.rx_pcn],
              ["RxGroup", report.insurance.rx_group],
              ["Effective", report.insurance.effective_date],
            ]
              .filter(([, v]) => v)
              .map(([label, v]) => (
                <div key={label}>
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
                  <div className="font-medium tabular-nums">{v}</div>
                </div>
              ))}
          </div>
          {report.insurance.notes && <p className="text-xs text-muted-foreground mt-2">{report.insurance.notes}</p>}
        </div>
      )}

      {/* Narrative */}
      {n && (
        <div className="report-section report-card bg-primary/5 rounded-xl border border-primary/20 p-5 space-y-2">
          <h2 className="font-semibold text-sm text-primary">Quarter in review</h2>
          <p className="text-sm font-medium">{n.headline}</p>
          {n.glucose_summary && <p className="text-sm text-muted-foreground">{n.glucose_summary}</p>}
          {n.cycle_summary && <p className="text-sm text-muted-foreground">{n.cycle_summary}</p>}
          {n.lifestyle_summary && <p className="text-sm text-muted-foreground">{n.lifestyle_summary}</p>}
          {n.discussion_points?.length > 0 && (
            <div className="pt-1">
              <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1">Points to discuss</p>
              <ul className="text-sm space-y-1 list-disc list-inside marker:text-primary">
                {n.discussion_points.map((p, idx) => <li key={idx}>{p}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* Glucose */}
      <div className="report-section space-y-3">
        <h2 className="font-semibold text-base">Glucose</h2>
        <DataQualityNote label="CGM" quality={g.quality} />
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Metric label="Average glucose" value={`${g.avg}`} sub="mg/dL" />
          <Metric label="GMI (est. A1c)" value={`${g.gmi}%`} tone={g.gmi >= 7 ? "text-amber-600" : "text-green-600"} />
          <Metric label="Time in range" value={`${g.tir}%`} sub="70–180 mg/dL" tone={g.tir >= 70 ? "text-green-600" : "text-amber-600"} />
          <Metric label="Variability (CV)" value={`${g.cv}%`} sub={g.cv <= 36 ? "stable" : "elevated"} tone={g.cv <= 36 ? "text-green-600" : "text-amber-600"} />
        </div>
        <div className="report-card bg-card rounded-lg border border-border p-4">
          <p className="text-xs font-medium mb-2">Time in ranges · {g.readings.toLocaleString()} readings over {g.days} days</p>
          <TIRBar g={g} />
        </div>
        <div className="report-card bg-card rounded-lg border border-border p-4">
          <p className="text-xs font-medium mb-1">Ambulatory Glucose Profile (typical day)</p>
          <p className="text-[11px] text-muted-foreground mb-2">Median (line) with 25–75% and 5–95% bands by time of day. Green dashes mark the 70–180 target.</p>
          <AGPChart agp={g.agp} />
        </div>
      </div>

      {/* Insulin */}
      {i.available && (
        <div className="report-section space-y-3">
          <h2 className="font-semibold text-base">Insulin & carbs</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {i.pump_reported_avg_tdd != null && (
              <Metric label="Pump-reported TDD" value={`${i.pump_reported_avg_tdd}`} sub={`${i.pump_reported_days} complete day${i.pump_reported_days === 1 ? "" : "s"} · ${Object.keys(i.pump_reported_sources || {}).join(", ")}`} />
            )}
            {i.calculated_avg_tdd != null && (
              <Metric label="Calculated TDD" value={`${i.calculated_avg_tdd}`} sub={`${i.calculated_days} full-coverage day${i.calculated_days === 1 ? "" : "s"}`} />
            )}
            <Metric label="Bolus insulin" value={`${i.avg_daily_bolus}`} sub={`${i.boluses_per_day}/day`} />
            {i.calculated_avg_daily_basal != null && <Metric label="Delivered basal (calc.)" value={`${i.calculated_avg_daily_basal}`} sub="units/day · full coverage only" />}
            {i.scheduled_avg_daily_basal != null && <Metric label="Programmed basal" value={`${i.scheduled_avg_daily_basal}`} sub="units/day · not confirmed delivery" />}
            <Metric label="Carbs logged" value={`${i.avg_daily_carbs}`} sub="g/day" />
          </div>
          <p className="text-[11px] text-muted-foreground">
            Pump-reported and calculated totals are kept separate. A calculated TDD is shown only for days with complete delivered-basal coverage; Glooko programmed basal is not treated as delivered. Carb totals reflect only logged entries.
          </p>
          <DataQualityNote label="Pump TDD" quality={i.quality} />
          <DataQualityNote label="Nutrition" quality={i.nutrition_quality} />
          {i.latest_activity_date && (
            <p className="text-[11px] text-muted-foreground">
              {i.latest_complete_date ? `Complete TDD data through ${i.latest_complete_date}. ` : "No complete TDD in this report period. "}
              Latest insulin activity: {i.latest_activity_date}.
            </p>
          )}
          {i.limitations?.map((limitation) => (
            <p key={limitation} className="text-[11px] text-muted-foreground">{limitation}</p>
          ))}
          {i.incomplete_days > 0 && (
            <p className="text-[11px] text-amber-700">{i.incomplete_days} day{i.incomplete_days === 1 ? "" : "s"} with insulin activity lacked a complete TDD and were excluded.</p>
          )}
          {i.discrepancy_days > 0 && (
            <p className="text-[11px] text-amber-700">{i.discrepancy_days} day{i.discrepancy_days === 1 ? "" : "s"} had a difference greater than rounding between pump-reported and calculated TDD; both values remain separate.</p>
          )}
        </div>
      )}

      {/* Cycle */}
      {c.available && (
        <div className="report-section space-y-3">
          <h2 className="font-semibold text-base">Menstrual cycle &amp; glucose</h2>
          <p className="text-xs text-muted-foreground">
            {c.cycles_detected} cycles{c.avg_cycle_length ? `, average ${c.avg_cycle_length} days` : ""} · phases {c.source}.
          </p>
          <DataQualityNote label="Cycle" quality={c.quality} />
          <div className="report-card bg-card rounded-lg border border-border overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-muted/50 text-xs text-muted-foreground">
                  <th className="text-left font-medium px-3 py-2">Phase</th>
                  <th className="text-right font-medium px-3 py-2">Days</th>
                  <th className="text-right font-medium px-3 py-2">Time in range</th>
                  <th className="text-right font-medium px-3 py-2">Avg glucose</th>
                </tr>
              </thead>
              <tbody>
                {["menstrual", "follicular", "ovulation", "luteal"].filter((p) => c.per_phase[p]).map((p) => {
                  const s = c.per_phase[p];
                  return (
                    <tr key={p} className="border-t border-border">
                      <td className="px-3 py-2 capitalize font-medium">{p}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{s.days}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{s.tir != null ? `${s.tir}%` : "—"}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{s.avg_glucose != null ? `${s.avg_glucose} mg/dL` : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Wellness */}
      {(w.oura || w.fitbit) && (
        <div className="report-section space-y-3">
          <h2 className="font-semibold text-base">Sleep, recovery &amp; activity</h2>
          {w.oura && <DataQualityNote label="Oura" quality={w.quality?.oura} />}
          {w.fitbit && <DataQualityNote label="Fitbit" quality={w.quality?.fitbit} />}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {w.oura?.avg_sleep_score != null && <Metric label="Sleep score (Oura)" value={w.oura.avg_sleep_score} sub="avg" />}
            {w.oura?.avg_readiness_score != null && <Metric label="Readiness (Oura)" value={w.oura.avg_readiness_score} sub="avg" />}
            {w.oura?.avg_resting_hr != null && <Metric label="Resting HR (Oura)" value={w.oura.avg_resting_hr} sub="bpm" />}
            {w.oura?.avg_spo2 != null && <Metric label="SpO₂ (Oura)" value={`${w.oura.avg_spo2}%`} />}
            {w.fitbit?.avg_steps != null && <Metric label="Steps (Fitbit)" value={w.fitbit.avg_steps.toLocaleString()} sub="avg/day" />}
            {w.fitbit?.avg_sleep_hours != null && <Metric label="Sleep (Fitbit)" value={`${w.fitbit.avg_sleep_hours}h`} sub="avg/night" />}
          </div>
        </div>
      )}

      {/* Labs */}
      {labs.available && (
        <div className="report-section space-y-3">
          <h2 className="font-semibold text-base">Labs</h2>
          {labs.flagged.length > 0 && (
            <div className="report-card bg-red-50 border border-red-200 rounded-lg p-3">
              <p className="text-xs font-semibold text-red-700 flex items-center gap-1 mb-1">
                <AlertTriangle className="w-3.5 h-3.5" /> Out of range at latest draw
              </p>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
                {labs.flagged.map((f) => (
                  <span key={f.test_name}>{f.test_name}: <b>{f.value} {f.unit}</b> <span className="text-red-600 uppercase text-[10px]">{f.flag}</span></span>
                ))}
              </div>
            </div>
          )}
          {Object.entries(labs.categories).map(([cat, tests]) => (
            <div key={cat} className="report-card bg-card rounded-lg border border-border overflow-hidden">
              <div className="bg-muted/50 px-3 py-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{cat}</div>
              <table className="w-full text-sm">
                <tbody>
                  {tests.map((t) => {
                    const TrendIcon = TREND_ICON[t.trend];
                    const outOfRange = t.flag && t.flag !== "normal" && t.flag !== "";
                    return (
                      <tr key={t.test_name} className="border-t border-border">
                        <td className="px-3 py-2 font-medium">{t.test_name}</td>
                        <td className={`px-3 py-2 text-right tabular-nums ${outOfRange ? "text-red-600 font-semibold" : ""}`}>
                          {t.value} <span className="text-muted-foreground font-normal">{t.unit}</span>
                        </td>
                        <td className="px-3 py-2 text-right text-xs text-muted-foreground tabular-nums">
                          {t.reference_low != null && t.reference_high != null ? `${t.reference_low}–${t.reference_high}` : ""}
                        </td>
                        <td className="px-3 py-2 text-center w-8">
                          {TrendIcon && <TrendIcon className={`w-3.5 h-3.5 inline ${t.trend === "up" ? "text-amber-500" : t.trend === "down" ? "text-sky-500" : "text-muted-foreground"}`} />}
                        </td>
                        <td className="px-3 py-2 text-right text-[11px] text-muted-foreground">
                          <div>Collected {t.event_time?.source_text || t.collected_date || "unknown"}</div>
                          {importDate(t.ingestion_time) && <div>Imported {importDate(t.ingestion_time)}</div>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      )}

      {/* Footer disclaimer */}
      <div className="report-section text-[11px] text-muted-foreground border-t border-border pt-4 leading-relaxed">
        This is an observational summary of self-tracked data generated for discussion with the care team. It is not a
        diagnosis, treatment recommendation, or medical advice. Glucose figures derive from CGM data; GMI is an estimate,
        not a laboratory A1c. Generated by GlucoPilot.
      </div>
    </div>
  );
}
