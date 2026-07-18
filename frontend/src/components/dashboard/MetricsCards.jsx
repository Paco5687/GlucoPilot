import { cn } from "@/lib/utils";
import { Target, TrendingUp, TrendingDown, BarChart3, Activity } from "lucide-react";

function MetricCard({ icon: Icon, label, value, unit, color, subtext }) {
  return (
    <div className="bg-card rounded-xl border border-border p-4 hover:shadow-sm transition-shadow">
      <div className="flex items-center gap-2 mb-3">
        <div className={cn("w-7 h-7 rounded-lg flex items-center justify-center", color)}>
          <Icon className="w-3.5 h-3.5" />
        </div>
        <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">{label}</span>
      </div>
      <div className="flex items-baseline gap-1.5">
        <span className="text-2xl font-bold font-mono tracking-tight">{value}</span>
        {unit && <span className="text-sm text-muted-foreground">{unit}</span>}
      </div>
      {subtext && <p className="text-xs text-muted-foreground mt-1">{subtext}</p>}
    </div>
  );
}

export default function MetricsCards({ readings }) {
  if (!readings.length) return null;

  const values = readings.map((r) => r.value);
  const avg = Math.round(values.reduce((s, v) => s + v, 0) / values.length);
  const inRange = readings.filter((r) => r.value >= 70 && r.value <= 180).length;
  const above = readings.filter((r) => r.value > 180).length;
  const below = readings.filter((r) => r.value < 70).length;
  const tirPct = Math.round((inRange / readings.length) * 100);
  const abovePct = Math.round((above / readings.length) * 100);
  const belowPct = Math.round((below / readings.length) * 100);

  const variance = values.reduce((s, v) => s + Math.pow(v - avg, 2), 0) / values.length;
  const sd = Math.round(Math.sqrt(variance));
  const cv = avg ? Math.round((sd / avg) * 100) : 0;

  return (
    <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
      <MetricCard
        icon={Target}
        label="Time in Range"
        value={tirPct}
        unit="%"
        color="bg-green-500/10 text-green-600"
        subtext="70–180 mg/dL"
      />
      <MetricCard
        icon={TrendingUp}
        label="Time Above"
        value={abovePct}
        unit="%"
        color="bg-amber-500/10 text-amber-600"
        subtext={`${above} readings > 180`}
      />
      <MetricCard
        icon={TrendingDown}
        label="Time Below"
        value={belowPct}
        unit="%"
        color="bg-red-500/10 text-red-600"
        subtext={`${below} readings < 70`}
      />
      <MetricCard
        icon={BarChart3}
        label="Average"
        value={avg}
        unit="mg/dL"
        color="bg-primary/10 text-primary"
        subtext={`GMI: ${(3.31 + 0.02392 * avg).toFixed(1)}%`}
      />
      <MetricCard
        icon={Activity}
        label="Variability"
        value={cv}
        unit="% CV"
        color={cn(cv > 36 ? "bg-amber-500/10 text-amber-600" : "bg-green-500/10 text-green-600")}
        subtext={`SD: ${sd} mg/dL`}
      />
    </div>
  );
}