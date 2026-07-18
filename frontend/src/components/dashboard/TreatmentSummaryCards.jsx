import { Syringe, Cookie, Pill, Hash } from "lucide-react";
import { cn } from "@/lib/utils";

function SummaryCard({ icon: Icon, label, value, unit, color }) {
  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center gap-2 mb-2">
        <div className={cn("w-6 h-6 rounded-lg flex items-center justify-center", color)}>
          <Icon className="w-3 h-3" />
        </div>
        <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">{label}</span>
      </div>
      <div className="flex items-baseline gap-1">
        <span className="text-xl font-bold font-mono">{value}</span>
        <span className="text-xs text-muted-foreground">{unit}</span>
      </div>
    </div>
  );
}

export default function TreatmentSummaryCards({ treatments }) {
  if (!treatments?.length) return null;

  const insulinTreatments = treatments.filter(
    (t) => t.type === "insulin" && t.event_type !== "Daily Total"
  );
  const carbTreatments = treatments.filter((t) => t.type === "carb");

  const totalInsulin = insulinTreatments.reduce((s, t) => s + (t.amount || 0), 0);
  const totalCarbs = carbTreatments.reduce((s, t) => s + (t.amount || 0), 0);
  const bolusCount = insulinTreatments.length;
  const avgBolus = bolusCount ? totalInsulin / bolusCount : 0;

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      <SummaryCard
        icon={Syringe}
        label="Total Insulin"
        value={totalInsulin.toFixed(1)}
        unit="units"
        color="bg-blue-500/10 text-blue-600"
      />
      <SummaryCard
        icon={Cookie}
        label="Total Carbs"
        value={Math.round(totalCarbs)}
        unit="g"
        color="bg-orange-500/10 text-orange-600"
      />
      <SummaryCard
        icon={Hash}
        label="Boluses"
        value={bolusCount}
        unit="doses"
        color="bg-purple-500/10 text-purple-600"
      />
      <SummaryCard
        icon={Pill}
        label="Avg Bolus"
        value={avgBolus.toFixed(2)}
        unit="U"
        color="bg-indigo-500/10 text-indigo-600"
      />
    </div>
  );
}