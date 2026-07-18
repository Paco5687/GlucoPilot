import { cn } from "@/lib/utils";

const HOURS = Array.from({ length: 24 }, (_, i) => i);
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function getHeatColor(avg) {
  if (!avg) return "bg-muted";
  if (avg < 70) return "bg-red-400";
  if (avg < 100) return "bg-green-300";
  if (avg <= 140) return "bg-green-500";
  if (avg <= 180) return "bg-green-300";
  if (avg <= 220) return "bg-amber-400";
  if (avg <= 260) return "bg-amber-500";
  return "bg-red-500";
}

export default function HeatmapChart({ readings }) {
  // Build a grid: day x hour -> average glucose
  const grid = {};
  readings.forEach((r) => {
    const d = new Date(r.timestamp);
    const day = d.getDay();
    const hour = d.getHours();
    const key = `${day}-${hour}`;
    if (!grid[key]) grid[key] = [];
    grid[key].push(r.value);
  });

  const averages = {};
  Object.entries(grid).forEach(([key, vals]) => {
    averages[key] = Math.round(vals.reduce((s, v) => s + v, 0) / vals.length);
  });

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <h3 className="text-sm font-semibold mb-4">Glucose Heatmap — Hour × Day</h3>
      <div className="overflow-x-auto">
        <div className="min-w-[600px]">
          {/* Hour labels */}
          <div className="flex gap-px ml-10 mb-1">
            {HOURS.map((h) => (
              <div key={h} className="flex-1 text-center text-[10px] text-muted-foreground">
                {h % 3 === 0 ? `${h}:00` : ""}
              </div>
            ))}
          </div>
          {/* Grid rows */}
          {DAYS.map((day, di) => (
            <div key={day} className="flex items-center gap-px mb-px">
              <div className="w-10 text-xs text-muted-foreground text-right pr-2">{day}</div>
              {HOURS.map((h) => {
                const key = `${di}-${h}`;
                const avg = averages[key];
                return (
                  <div
                    key={h}
                    className={cn(
                      "flex-1 h-6 rounded-sm transition-colors cursor-default",
                      getHeatColor(avg)
                    )}
                    title={avg ? `${day} ${h}:00 — Avg: ${avg} mg/dL` : `${day} ${h}:00 — No data`}
                  />
                );
              })}
            </div>
          ))}
          {/* Legend */}
          <div className="flex items-center justify-center gap-2 mt-3 text-[10px] text-muted-foreground">
            <span>Low</span>
            <div className="flex gap-0.5">
              <div className="w-4 h-3 rounded-sm bg-red-400" />
              <div className="w-4 h-3 rounded-sm bg-green-300" />
              <div className="w-4 h-3 rounded-sm bg-green-500" />
              <div className="w-4 h-3 rounded-sm bg-amber-400" />
              <div className="w-4 h-3 rounded-sm bg-amber-500" />
              <div className="w-4 h-3 rounded-sm bg-red-500" />
            </div>
            <span>High</span>
          </div>
        </div>
      </div>
    </div>
  );
}