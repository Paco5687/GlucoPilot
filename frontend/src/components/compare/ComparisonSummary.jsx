import { MessageSquare } from "lucide-react";

export default function ComparisonSummary({ current, previous, currentLabel, previousLabel }) {
  if (!current || !previous) return null;

  const statements = [];

  const tirDiff = current.tir - previous.tir;
  if (Math.abs(tirDiff) >= 1) {
    statements.push(
      tirDiff > 0
        ? `Time in range improved by ${tirDiff.toFixed(0)}% compared to ${previousLabel.toLowerCase()}.`
        : `Time in range decreased by ${Math.abs(tirDiff).toFixed(0)}% compared to ${previousLabel.toLowerCase()}.`
    );
  }

  const avgDiff = current.avg - previous.avg;
  if (Math.abs(avgDiff) >= 3) {
    statements.push(
      avgDiff < 0
        ? `Average glucose dropped by ${Math.abs(avgDiff).toFixed(0)} mg/dL — a positive trend.`
        : `Average glucose rose by ${avgDiff.toFixed(0)} mg/dL compared to the previous period.`
    );
  }

  const cvDiff = current.cv - previous.cv;
  if (Math.abs(cvDiff) >= 2) {
    statements.push(
      cvDiff < 0
        ? `Glucose variability decreased by ${Math.abs(cvDiff).toFixed(0)}% — readings are more consistent.`
        : `Glucose variability increased by ${cvDiff.toFixed(0)}% — readings are more spread out.`
    );
  }

  const aboveDiff = current.above - previous.above;
  if (Math.abs(aboveDiff) >= 2) {
    statements.push(
      aboveDiff < 0
        ? `Time above range reduced by ${Math.abs(aboveDiff).toFixed(0)}%.`
        : `Time above range increased by ${aboveDiff.toFixed(0)}%.`
    );
  }

  if (statements.length === 0) {
    statements.push("No significant changes detected between these periods.");
  }

  return (
    <div className="bg-primary/5 border border-primary/20 rounded-xl p-5">
      <div className="flex items-center gap-2 mb-3">
        <MessageSquare className="w-4 h-4 text-primary" />
        <h3 className="text-sm font-semibold text-primary">Summary</h3>
      </div>
      <ul className="space-y-2">
        {statements.map((s, i) => (
          <li key={i} className="text-sm text-foreground leading-relaxed flex items-start gap-2">
            <span className="text-primary mt-1">•</span>
            {s}
          </li>
        ))}
      </ul>
    </div>
  );
}