import { TrendingUp, TrendingDown, Minus } from "lucide-react";

export default function OuraScoreCard({ icon: Icon, label, score, prevScore, color, bgColor, detail, unit }) {
  const diff = score != null && prevScore != null ? score - prevScore : null;

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center gap-2 mb-2">
        <div className={`w-7 h-7 rounded-lg flex items-center justify-center ${bgColor}`}>
          <Icon className={`w-3.5 h-3.5 ${color}`} />
        </div>
        <span className="text-xs text-muted-foreground font-medium">{label}</span>
      </div>
      <div className="flex items-end gap-1.5">
        <span className="text-2xl font-bold">
          {score != null ? score : "—"}
        </span>
        {unit && score != null && (
          <span className="text-xs text-muted-foreground mb-1">{unit}</span>
        )}
      </div>
      <div className="flex items-center justify-between mt-1">
        {detail && (
          <span className="text-xs text-muted-foreground">{detail}</span>
        )}
        {diff != null && (
          <span className={`text-xs flex items-center gap-0.5 ml-auto ${diff > 0 ? "text-emerald-500" : diff < 0 ? "text-red-500" : "text-muted-foreground"}`}>
            {diff > 0 ? <TrendingUp className="w-3 h-3" /> : diff < 0 ? <TrendingDown className="w-3 h-3" /> : <Minus className="w-3 h-3" />}
            {diff > 0 ? "+" : ""}{diff}
          </span>
        )}
      </div>
    </div>
  );
}