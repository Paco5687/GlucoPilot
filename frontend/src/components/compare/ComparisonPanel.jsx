import { cn } from "@/lib/utils";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";

function CompareRow({ label, currentVal, previousVal, unit, isPercentage, invertBetter }) {
  const diff = currentVal - previousVal;
  const isBetter = invertBetter ? diff < 0 : diff > 0;
  const isNeutral = Math.abs(diff) < 0.5;

  return (
    <div className="flex items-center justify-between py-3 border-b border-border last:border-0">
      <span className="text-sm text-muted-foreground">{label}</span>
      <div className="flex items-center gap-4">
        <span className="text-sm font-mono text-muted-foreground">
          {previousVal}{unit}
        </span>
        <div className={cn(
          "flex items-center gap-1 text-sm font-medium",
          isNeutral ? "text-muted-foreground" : isBetter ? "text-green-600" : "text-red-500"
        )}>
          {isNeutral ? (
            <Minus className="w-3.5 h-3.5" />
          ) : isBetter ? (
            <TrendingUp className="w-3.5 h-3.5" />
          ) : (
            <TrendingDown className="w-3.5 h-3.5" />
          )}
          <span>{diff > 0 ? "+" : ""}{isPercentage ? diff.toFixed(0) : diff.toFixed(1)}{unit}</span>
        </div>
        <span className="text-sm font-mono font-semibold w-16 text-right">
          {currentVal}{unit}
        </span>
      </div>
    </div>
  );
}

export default function ComparisonPanel({ current, previous, currentLabel, previousLabel }) {
  if (!current || !previous) return null;

  return (
    <div className="bg-card rounded-xl border border-border p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold">{currentLabel} vs {previousLabel}</h3>
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span>{previousLabel}</span>
          <span>→</span>
          <span className="font-medium text-foreground">{currentLabel}</span>
        </div>
      </div>
      <div>
        <CompareRow label="Time in Range" currentVal={current.tir} previousVal={previous.tir} unit="%" isPercentage />
        <CompareRow label="Time Above" currentVal={current.above} previousVal={previous.above} unit="%" isPercentage invertBetter />
        <CompareRow label="Time Below" currentVal={current.below} previousVal={previous.below} unit="%" isPercentage invertBetter />
        <CompareRow label="Average Glucose" currentVal={current.avg} previousVal={previous.avg} unit=" mg/dL" invertBetter />
        <CompareRow label="Variability (CV)" currentVal={current.cv} previousVal={previous.cv} unit="%" isPercentage invertBetter />
      </div>
    </div>
  );
}