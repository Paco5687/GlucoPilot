import { cn } from "@/lib/utils";
import { TREND_ARROWS, TREND_LABELS, getGlucoseLabel, getGlucoseBgClass, formatTimeSince } from "@/lib/glucoseUtils";
import { Activity } from "lucide-react";

export default function CurrentGlucose({ reading, previousReading }) {
  if (!reading) {
    return (
      <div className="bg-card rounded-2xl border border-border p-8 text-center">
        <Activity className="w-8 h-8 text-muted-foreground mx-auto mb-3" />
        <p className="text-muted-foreground">No glucose data available</p>
      </div>
    );
  }

  const delta = previousReading ? reading.value - previousReading.value : 0;
  const deltaSign = delta > 0 ? "+" : "";
  const trendArrow = TREND_ARROWS[reading.trend] || TREND_ARROWS.Unknown;
  const trendLabel = TREND_LABELS[reading.trend] || "Unknown";

  return (
    <div className={cn("rounded-2xl border p-6 transition-all", getGlucoseBgClass(reading.value))}>
      <div className="flex items-start justify-between mb-2">
        <span className="text-xs font-medium uppercase tracking-wider opacity-70">Current Glucose</span>
        <span className="text-xs opacity-60">{formatTimeSince(reading.timestamp)}</span>
      </div>

      <div className="flex items-baseline gap-3 mb-1">
        <span className="text-6xl font-bold font-mono tracking-tighter">{reading.value}</span>
        <div className="flex flex-col">
          <span className="text-3xl">{trendArrow}</span>
          <span className="text-xs font-medium opacity-70">mg/dL</span>
        </div>
      </div>

      <div className="flex items-center gap-4 mt-3">
        <span className="text-sm font-medium">{getGlucoseLabel(reading.value)}</span>
        <span className="text-sm opacity-70">{trendLabel}</span>
        {delta !== 0 && (
          <span className="text-sm font-mono opacity-70">
            {deltaSign}{delta} mg/dL
          </span>
        )}
      </div>
    </div>
  );
}