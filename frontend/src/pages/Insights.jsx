import { useState, useEffect } from "react";
import { base44 } from "@/api/base44Client";
import { useAuth } from "@/lib/AuthContext";
import { Button } from "@/components/ui/button";
import SafetyBanner from "../components/SafetyBanner";
import { Lightbulb, Loader2, RefreshCw, TrendingUp, AlertTriangle, Info, Sparkles } from "lucide-react";
import { toast } from "sonner";

const SEVERITY_STYLES = {
  positive: { icon: TrendingUp, chip: "bg-green-100 text-green-700", label: "Positive" },
  warning: { icon: AlertTriangle, chip: "bg-amber-100 text-amber-700", label: "Worth watching" },
  alert: { icon: AlertTriangle, chip: "bg-red-100 text-red-700", label: "Alert" },
  info: { icon: Info, chip: "bg-sky-100 text-sky-700", label: "Info" },
};

const CATEGORY_LABELS = {
  time_in_range: "Time in range",
  variability: "Variability",
  patterns: "Patterns",
  comparison: "Comparison",
  general: "General",
};

export default function Insights() {
  const { isAdmin } = useAuth();
  const [insights, setInsights] = useState([]);
  const [loading, setLoading] = useState(true);
  const [analyzing, setAnalyzing] = useState(false);

  useEffect(() => {
    load();
  }, []);

  async function load() {
    setLoading(true);
    try {
      const rows = await base44.entities.Insight.list("-date_generated", 100);
      setInsights(rows);
    } catch {
      setInsights([]);
    }
    setLoading(false);
  }

  async function analyze() {
    setAnalyzing(true);
    try {
      const res = await base44.functions.invoke("analyzeInsights", {});
      if (res.data?.message) toast.info(res.data.message);
      else toast.success(`Analysis complete — ${res.data.insightsFound} insights found`);
      await load();
    } catch (err) {
      toast.error(err?.response?.data?.error || err.message || "Analysis failed");
    }
    setAnalyzing(false);
  }

  return (
    <div className="space-y-6">
      <SafetyBanner />

      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold">Insights</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Cross-domain relationships: glucose × sleep × readiness × activity × cycle, over the last 90 days.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {isAdmin && (
            <Button onClick={analyze} disabled={analyzing} className="gap-2">
              {analyzing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
              {analyzing ? "Analyzing…" : "Analyze now"}
            </Button>
          )}
          <Lightbulb className="w-6 h-6 text-primary" />
        </div>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-muted-foreground text-sm">
          <Loader2 className="w-4 h-4 animate-spin" /> Loading…
        </div>
      ) : insights.length === 0 ? (
        <div className="text-center py-16 text-muted-foreground">
          <Lightbulb className="w-10 h-10 mx-auto mb-3 opacity-40" />
          <p className="text-sm">No insights yet. Hit "Analyze now" to look for relationships in the data.</p>
        </div>
      ) : (
        <div className="grid md:grid-cols-2 gap-4">
          {insights.map((ins) => {
            const style = SEVERITY_STYLES[ins.severity] || SEVERITY_STYLES.info;
            const Icon = style.icon;
            return (
              <div key={ins.id} className="bg-card rounded-xl border border-border p-5">
                <div className="flex items-center gap-2 mb-2 flex-wrap">
                  <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium flex items-center gap-1 ${style.chip}`}>
                    <Icon className="w-3 h-3" /> {style.label}
                  </span>
                  <span className="text-[10px] px-2 py-0.5 rounded-full font-medium bg-muted text-muted-foreground">
                    {CATEGORY_LABELS[ins.category] || ins.category}
                  </span>
                </div>
                <h3 className="font-semibold text-sm mb-1">{ins.title}</h3>
                {ins.description && <p className="text-sm text-muted-foreground leading-relaxed">{ins.description}</p>}
              </div>
            );
          })}
        </div>
      )}

      {insights.length > 0 && (
        <p className="text-xs text-muted-foreground flex items-center gap-1.5">
          <RefreshCw className="w-3 h-3" />
          Generated {insights[0]?.date_generated ? new Date(insights[0].date_generated).toLocaleString() : ""}. Educational
          only — correlation is not causation; discuss anything actionable with the healthcare team.
        </p>
      )}
    </div>
  );
}
