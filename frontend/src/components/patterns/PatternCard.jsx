import { cn } from "@/lib/utils";
import { AlertTriangle, TrendingUp, Moon, Clock, Zap, ArrowDown } from "lucide-react";

const PATTERN_ICONS = {
  recurring_high: TrendingUp,
  post_meal_spike: Zap,
  ineffective_correction: AlertTriangle,
  overnight_drift: Moon,
  insulin_resistance: Clock,
  recurring_low: ArrowDown,
  dawn_phenomenon: TrendingUp,
};

const CONFIDENCE_STYLES = {
  high: "bg-red-100 text-red-700 border-red-200",
  medium: "bg-amber-100 text-amber-700 border-amber-200",
  low: "bg-muted text-muted-foreground border-border",
};

const TIME_LABELS = {
  morning: "🌅 Morning",
  afternoon: "☀️ Afternoon",
  evening: "🌆 Evening",
  overnight: "🌙 Overnight",
  all_day: "📅 All Day",
};

export default function PatternCard({ pattern }) {
  const Icon = PATTERN_ICONS[pattern.pattern_type] || AlertTriangle;
  let evidence = [];
  try {
    evidence = JSON.parse(pattern.supporting_evidence || "[]");
  } catch {}

  return (
    <div className="bg-card rounded-xl border border-border p-5 hover:shadow-md transition-shadow">
      <div className="flex items-start gap-3 mb-3">
        <div className="w-9 h-9 rounded-lg bg-primary/10 flex items-center justify-center flex-shrink-0">
          <Icon className="w-4.5 h-4.5 text-primary" />
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="font-semibold text-sm leading-tight">{pattern.title}</h3>
          <div className="flex items-center gap-2 mt-1">
            <span className={cn("text-[10px] px-2 py-0.5 rounded-full border font-medium", CONFIDENCE_STYLES[pattern.confidence])}>
              {pattern.confidence} confidence
            </span>
            {pattern.time_of_day && (
              <span className="text-xs text-muted-foreground">{TIME_LABELS[pattern.time_of_day]}</span>
            )}
          </div>
        </div>
      </div>
      <p className="text-sm text-muted-foreground leading-relaxed mb-3">{pattern.explanation}</p>
      {evidence.length > 0 && (
        <div className="bg-muted/50 rounded-lg p-3">
          <p className="text-[10px] font-medium text-muted-foreground uppercase tracking-wider mb-2">Supporting Evidence</p>
          <ul className="space-y-1">
            {evidence.map((e, i) => (
              <li key={i} className="text-xs text-muted-foreground flex items-start gap-1.5">
                <span className="text-primary mt-0.5">•</span>
                {e}
              </li>
            ))}
          </ul>
        </div>
      )}
      {pattern.occurrences && (
        <p className="text-xs text-muted-foreground mt-3">
          Detected {pattern.occurrences} times
        </p>
      )}
    </div>
  );
}