import { useState, useEffect, useCallback } from "react";
import { base44 } from "@/api/base44Client";
import { useAuth } from "@/lib/AuthContext";
import { Button } from "@/components/ui/button";
import SafetyBanner from "../components/SafetyBanner";
import PatternCard from "../components/patterns/PatternCard";
import { Sparkles, Loader2, RefreshCw, Link2, CheckCircle2, AlertTriangle, Brain } from "lucide-react";
import { toast } from "sonner";

const DOMAIN_STYLE = {
  glucose: "bg-blue-100 text-blue-700",
  labs: "bg-purple-100 text-purple-700",
  cycle: "bg-pink-100 text-pink-700",
  wearables: "bg-teal-100 text-teal-700",
  imaging: "bg-amber-100 text-amber-700",
};

const PATTERN_FILTERS = [
  { value: "all", label: "All" },
  { value: "recurring_high", label: "Recurring Highs" },
  { value: "recurring_low", label: "Recurring Lows" },
  { value: "post_meal_spike", label: "Post-Meal Spikes" },
  { value: "ineffective_correction", label: "Ineffective Corrections" },
  { value: "overnight_drift", label: "Overnight Drift" },
  { value: "dawn_phenomenon", label: "Dawn Phenomenon" },
  { value: "insulin_resistance", label: "Insulin Resistance" },
];

function timeAgo(iso) {
  if (!iso) return "never";
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 3600) return `${Math.max(1, Math.round(d / 60))} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return `${Math.round(d / 86400)} d ago`;
}

export default function Overview() {
  const { isAdmin } = useAuth();
  const [summary, setSummary] = useState(null);
  const [patterns, setPatterns] = useState([]);
  const [patternFilter, setPatternFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    const [s, p] = await Promise.allSettled([
      base44.functions.invoke("healthSummary", { action: "get" }),
      base44.entities.Pattern.list("-created_date", 50),
    ]);
    setSummary(s.status === "fulfilled" ? (s.value.data?.summary || null) : null);
    setPatterns(p.status === "fulfilled" ? (p.value || []) : []);
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  async function handleRefresh() {
    setGenerating(true);
    toast.info("Synthesizing across all your data and re-scanning patterns — a minute or two…");
    try {
      const [s] = await Promise.allSettled([
        base44.functions.invoke("healthSummary", { action: "generate" }),
        base44.functions.invoke("analyzePatterns", {}),
      ]);
      if (s.status === "fulfilled" && s.value.data?.error) throw new Error(s.value.data.error);
      await load();
      toast.success("Overview refreshed.");
    } catch (err) {
      toast.error(err?.message || "Refresh failed");
    }
    setGenerating(false);
  }

  const filteredPatterns = patternFilter === "all" ? patterns : patterns.filter((p) => p.pattern_type === patternFilter);

  return (
    <div className="space-y-6">
      <SafetyBanner />

      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2">
            <Sparkles className="w-5 h-5 text-primary" /> Health Overview
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            Connections and patterns across glucose, labs, cycle, wearables, and imaging — observational, not medical advice.
            {summary?.generated_at && <span> · updated {timeAgo(summary.generated_at)}</span>}
          </p>
        </div>
        {isAdmin && (
          <Button variant="outline" size="sm" onClick={handleRefresh} disabled={generating} className="gap-2">
            {generating ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            {generating ? "Refreshing…" : "Refresh"}
          </Button>
        )}
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-40"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>
      ) : (
        <>
          {!summary ? (
            <div className="bg-card rounded-xl border border-border p-8 text-center">
              <Sparkles className="w-8 h-8 mx-auto mb-2 text-muted-foreground" />
              <p className="text-sm text-muted-foreground mb-3">
                No summary yet. It refreshes automatically each week — or generate one now.
              </p>
              {isAdmin && <Button size="sm" onClick={handleRefresh} disabled={generating}>{generating ? "Generating…" : "Generate overview"}</Button>}
            </div>
          ) : (
            <>
              {summary.headline && (
                <div className="report-card bg-primary/5 rounded-xl border border-primary/20 p-5">
                  <p className="text-base font-medium">{summary.headline}</p>
                </div>
              )}

              {summary.metrics && (
                <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
                  {[
                    { label: "Time in range", value: summary.metrics.tir, unit: "%", round: 0 },
                    { label: "Avg glucose", value: summary.metrics.avg, unit: "mg/dL", round: 0 },
                    { label: "GMI", value: summary.metrics.gmi, unit: "%", round: 1 },
                    { label: "HRV", value: summary.metrics.hrv, unit: "ms", round: 0 },
                    { label: "Resting HR", value: summary.metrics.resting_hr, unit: "bpm", round: 0 },
                    { label: "BMI", value: summary.metrics.bmi, unit: "", round: 1 },
                  ].filter((m) => m.value != null).map((m) => (
                    <div key={m.label} className="bg-card rounded-xl border border-border p-3">
                      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{m.label}</div>
                      <div className="text-lg font-semibold tabular-nums">{Number(m.value).toFixed(m.round)}<span className="text-xs font-normal text-muted-foreground ml-0.5">{m.unit}</span></div>
                    </div>
                  ))}
                  {summary.metrics.labs_out_of_range != null && summary.metrics.labs_out_of_range > 0 && (
                    <div className="bg-card rounded-xl border border-border p-3">
                      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Labs out of range</div>
                      <div className="text-lg font-semibold tabular-nums text-red-600">{summary.metrics.labs_out_of_range}</div>
                    </div>
                  )}
                </div>
              )}

              {summary.observations?.length > 0 && (
                <div className="space-y-3">
                  <h2 className="font-semibold text-base flex items-center gap-2"><Link2 className="w-4 h-4 text-primary" /> Connections worth noticing</h2>
                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                    {summary.observations.map((o, i) => (
                      <div key={i} className="bg-card rounded-xl border border-border p-4">
                        <h3 className="font-semibold text-sm mb-1">{o.title}</h3>
                        <p className="text-sm text-muted-foreground leading-relaxed">{o.detail}</p>
                        {o.domains?.length > 0 && (
                          <div className="flex flex-wrap gap-1.5 mt-2">
                            {o.domains.map((d) => (
                              <span key={d} className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${DOMAIN_STYLE[d?.toLowerCase()] || "bg-muted text-muted-foreground"}`}>{d}</span>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {summary.working?.length > 0 && (
                  <div className="bg-card rounded-xl border border-border p-4">
                    <h3 className="font-semibold text-sm flex items-center gap-2 mb-2 text-emerald-600"><CheckCircle2 className="w-4 h-4" /> On track</h3>
                    <ul className="space-y-1.5 text-sm text-muted-foreground list-disc list-inside marker:text-emerald-500">
                      {summary.working.map((w, i) => <li key={i}>{w}</li>)}
                    </ul>
                  </div>
                )}
                {summary.watch?.length > 0 && (
                  <div className="bg-card rounded-xl border border-border p-4">
                    <h3 className="font-semibold text-sm flex items-center gap-2 mb-2 text-amber-600"><AlertTriangle className="w-4 h-4" /> Worth watching</h3>
                    <ul className="space-y-1.5 text-sm text-muted-foreground list-disc list-inside marker:text-amber-500">
                      {summary.watch.map((w, i) => <li key={i}>{w}</li>)}
                    </ul>
                  </div>
                )}
              </div>
            </>
          )}

          {/* Glucose patterns (folded in from the old Patterns tab) */}
          {patterns.length > 0 && (
            <div className="space-y-3 pt-2">
              <h2 className="font-semibold text-base flex items-center gap-2">
                <Brain className="w-4 h-4 text-primary" /> Patterns in your glucose
                <span className="text-xs font-normal text-muted-foreground">{patterns.length}</span>
              </h2>
              <div className="flex flex-wrap gap-2">
                {PATTERN_FILTERS.filter((o) => o.value === "all" || patterns.some((p) => p.pattern_type === o.value)).map((opt) => (
                  <button key={opt.value} onClick={() => setPatternFilter(opt.value)}
                    className={`px-3 py-1.5 rounded-full text-xs font-medium transition-colors ${patternFilter === opt.value ? "bg-primary text-primary-foreground" : "bg-secondary text-secondary-foreground hover:bg-accent"}`}>
                    {opt.label}
                  </button>
                ))}
              </div>
              {filteredPatterns.length === 0 ? (
                <p className="text-sm text-muted-foreground">No patterns match this filter.</p>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  {filteredPatterns.map((p) => <PatternCard key={p.id} pattern={p} />)}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
