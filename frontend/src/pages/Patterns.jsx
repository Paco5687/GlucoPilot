import { useState, useEffect } from "react";
import { base44 } from "@/api/base44Client";
import { useAuth } from "@/lib/AuthContext";
import { useViewingData } from "@/hooks/useViewingData";
import PatternCard from "../components/patterns/PatternCard";
import SafetyBanner from "../components/SafetyBanner";
import { Button } from "@/components/ui/button";
import { Brain, Loader2, Sparkles } from "lucide-react";
import { toast } from "sonner";

const FILTER_OPTIONS = [
  { value: "all", label: "All Patterns" },
  { value: "recurring_high", label: "Recurring Highs" },
  { value: "post_meal_spike", label: "Post-Meal Spikes" },
  { value: "ineffective_correction", label: "Ineffective Corrections" },
  { value: "overnight_drift", label: "Overnight Drift" },
  { value: "insulin_resistance", label: "Insulin Resistance" },
  { value: "recurring_low", label: "Recurring Lows" },
  { value: "dawn_phenomenon", label: "Dawn Phenomenon" },
];

export default function Patterns() {
  const { isAdmin } = useAuth();
  const [patterns, setPatterns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [analyzing, setAnalyzing] = useState(false);
  const [filter, setFilter] = useState("all");
  const { fetchEntity, isViewingShared, viewingEmail } = useViewingData();

  const loadPatterns = async () => {
    const data = await fetchEntity("Pattern", "-created_date", 50);
    setPatterns(data);
    setLoading(false);
  };

  useEffect(() => {
    setLoading(true);
    loadPatterns();
  }, [isViewingShared, viewingEmail]);

  const runAnalysis = async () => {
    setAnalyzing(true);
    const res = await base44.functions.invoke('analyzePatterns', {});
    toast.success(`Analysis complete — ${res.data.patternsFound} patterns found`);
    await loadPatterns();
    setAnalyzing(false);
  };

  const filtered = filter === "all" ? patterns : patterns.filter((p) => p.pattern_type === filter);

  const highConfidence = patterns.filter((p) => p.confidence === "high").length;
  const mediumConfidence = patterns.filter((p) => p.confidence === "medium").length;

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-6 h-6 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <SafetyBanner />

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold">Pattern Detection</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {patterns.length} patterns detected — {highConfidence} high confidence, {mediumConfidence} medium
          </p>
        </div>
        {!isViewingShared && isAdmin && (
          <Button onClick={runAnalysis} disabled={analyzing} className="gap-2">
            {analyzing ? (
              <><Loader2 className="w-4 h-4 animate-spin" /> Analyzing...</>
            ) : (
              <><Sparkles className="w-4 h-4" /> Analyze Now</>
            )}
          </Button>
        )}
      </div>

      {/* Filter chips */}
      <div className="flex flex-wrap gap-2">
        {FILTER_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            onClick={() => setFilter(opt.value)}
            className={`px-3 py-1.5 rounded-full text-xs font-medium transition-all ${
              filter === opt.value
                ? "bg-primary text-primary-foreground"
                : "bg-secondary text-secondary-foreground hover:bg-accent"
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {/* Pattern cards */}
      {filtered.length === 0 ? (
        <div className="bg-card rounded-xl border border-border p-12 text-center">
          <Brain className="w-10 h-10 text-muted-foreground mx-auto mb-3" />
          <p className="text-muted-foreground text-sm">No patterns found for this filter.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {filtered.map((pattern) => (
            <PatternCard key={pattern.id} pattern={pattern} />
          ))}
        </div>
      )}
    </div>
  );
}