import { useMemo } from "react";
import { Moon, Activity, Flame, TrendingUp, TrendingDown, Minus } from "lucide-react";

function computeCorrelation(pairs) {
  if (pairs.length < 5) return null;
  const n = pairs.length;
  const sumX = pairs.reduce((s, p) => s + p.x, 0);
  const sumY = pairs.reduce((s, p) => s + p.y, 0);
  const sumXY = pairs.reduce((s, p) => s + p.x * p.y, 0);
  const sumX2 = pairs.reduce((s, p) => s + p.x * p.x, 0);
  const sumY2 = pairs.reduce((s, p) => s + p.y * p.y, 0);
  const num = n * sumXY - sumX * sumY;
  const den = Math.sqrt((n * sumX2 - sumX * sumX) * (n * sumY2 - sumY * sumY));
  if (den === 0) return null;
  return num / den;
}

function buildInsight(label, r, highLabel, lowLabel) {
  if (r == null) return null;
  const strength = Math.abs(r) >= 0.5 ? "strong" : Math.abs(r) >= 0.3 ? "moderate" : Math.abs(r) >= 0.15 ? "weak" : null;
  if (!strength) return null;
  const direction = r > 0 ? highLabel : lowLabel;
  return { label, r, strength, direction };
}

function CorrelationCard({ icon: Icon, label, color, bgColor, insight, pairs }) {
  if (!insight) return null;

  // Compute group comparison
  const sorted = [...pairs].sort((a, b) => a.x - b.x);
  const bottom = sorted.slice(0, Math.floor(sorted.length / 3));
  const top = sorted.slice(-Math.floor(sorted.length / 3));
  const avgLow = bottom.length ? Math.round(bottom.reduce((s, p) => s + p.y, 0) / bottom.length) : null;
  const avgHigh = top.length ? Math.round(top.reduce((s, p) => s + p.y, 0) / top.length) : null;
  const diff = avgHigh != null && avgLow != null ? avgHigh - avgLow : null;

  const strengthColors = {
    strong: "text-primary font-semibold",
    moderate: "text-foreground font-medium",
    weak: "text-muted-foreground",
  };

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center gap-2 mb-2">
        <div className={`w-7 h-7 rounded-lg flex items-center justify-center ${bgColor}`}>
          <Icon className={`w-3.5 h-3.5 ${color}`} />
        </div>
        <span className="text-xs font-medium">{label} × Glucose</span>
        <span className={`text-[10px] px-1.5 py-0.5 rounded-full ml-auto ${
          insight.strength === "strong" ? "bg-primary/10 text-primary" :
          insight.strength === "moderate" ? "bg-accent text-accent-foreground" :
          "bg-muted text-muted-foreground"
        }`}>
          {insight.strength}
        </span>
      </div>
      <p className={`text-sm ${strengthColors[insight.strength]}`}>
        {insight.direction}
      </p>
      {diff != null && (
        <div className="flex items-center gap-1 mt-2 text-xs text-muted-foreground">
          {diff > 0 ? <TrendingUp className="w-3 h-3 text-amber-500" /> : diff < 0 ? <TrendingDown className="w-3 h-3 text-emerald-500" /> : <Minus className="w-3 h-3" />}
          <span>
            Top vs bottom third: {diff > 0 ? "+" : ""}{diff}% TIR difference
          </span>
        </div>
      )}
      <div className="text-[10px] text-muted-foreground mt-1">
        r = {insight.r.toFixed(2)} · {pairs.length} days analyzed
      </div>
    </div>
  );
}

export default function CorrelationCards({ readings, ouraData }) {
  const analysis = useMemo(() => {
    if (!readings?.length || !ouraData?.length) return null;

    // Build daily glucose TIR
    const glucoseByDay = {};
    readings.forEach((r) => {
      const day = new Date(r.timestamp).toISOString().split("T")[0];
      if (!glucoseByDay[day]) glucoseByDay[day] = [];
      glucoseByDay[day].push(r.value);
    });
    const tirByDay = {};
    for (const [day, vals] of Object.entries(glucoseByDay)) {
      if (vals.length < 10) continue; // skip sparse days
      tirByDay[day] = Math.round((vals.filter((v) => v >= 70 && v <= 180).length / vals.length) * 100);
    }

    const ouraByDay = {};
    ouraData.forEach((d) => { ouraByDay[d.date] = d; });

    // Build paired data for each metric
    const sleepPairs = [];
    const readinessPairs = [];
    const activityPairs = [];

    for (const [day, tir] of Object.entries(tirByDay)) {
      const o = ouraByDay[day];
      if (!o) continue;
      if (o.sleep_score != null) sleepPairs.push({ x: o.sleep_score, y: tir });
      if (o.readiness_score != null) readinessPairs.push({ x: o.readiness_score, y: tir });
      if (o.activity_score != null) activityPairs.push({ x: o.activity_score, y: tir });
    }

    const sleepR = computeCorrelation(sleepPairs);
    const readinessR = computeCorrelation(readinessPairs);
    const activityR = computeCorrelation(activityPairs);

    return {
      sleep: {
        insight: buildInsight("Sleep", sleepR,
          "Better sleep scores tend to come with higher Time in Range",
          "Lower sleep scores tend to come with higher Time in Range"
        ),
        pairs: sleepPairs,
      },
      readiness: {
        insight: buildInsight("Readiness", readinessR,
          "Higher readiness scores are associated with better glucose control",
          "Lower readiness scores appear linked to better glucose days"
        ),
        pairs: readinessPairs,
      },
      activity: {
        insight: buildInsight("Activity", activityR,
          "More active days correlate with better Time in Range",
          "Higher activity days appear linked to lower Time in Range"
        ),
        pairs: activityPairs,
      },
    };
  }, [readings, ouraData]);

  if (!analysis) return null;
  const hasInsights = analysis.sleep.insight || analysis.readiness.insight || analysis.activity.insight;
  if (!hasInsights) return null;

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-semibold flex items-center gap-2">
        💡 Glucose × Oura Insights
      </h3>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <CorrelationCard
          icon={Moon}
          label="Sleep"
          color="text-indigo-500"
          bgColor="bg-indigo-500/10"
          insight={analysis.sleep.insight}
          pairs={analysis.sleep.pairs}
        />
        <CorrelationCard
          icon={Activity}
          label="Readiness"
          color="text-emerald-500"
          bgColor="bg-emerald-500/10"
          insight={analysis.readiness.insight}
          pairs={analysis.readiness.pairs}
        />
        <CorrelationCard
          icon={Flame}
          label="Activity"
          color="text-orange-500"
          bgColor="bg-orange-500/10"
          insight={analysis.activity.insight}
          pairs={analysis.activity.pairs}
        />
      </div>
    </div>
  );
}