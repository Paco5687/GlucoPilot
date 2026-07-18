import { useMemo } from "react";
import { parseISO, differenceInDays } from "date-fns";
import { Activity, Calendar, Droplets } from "lucide-react";

export default function CycleStats({ logs }) {
  const stats = useMemo(() => {
    const menstrualDays = logs
      .filter((l) => l.phase === "menstrual" || (l.flow && l.flow !== "none"))
      .map((l) => parseISO(l.date))
      .sort((a, b) => a - b);

    if (menstrualDays.length < 2) return null;

    // Find period start days (gaps of 5+ days between menstrual days = new period)
    const periodStarts = [menstrualDays[0]];
    for (let i = 1; i < menstrualDays.length; i++) {
      if (differenceInDays(menstrualDays[i], menstrualDays[i - 1]) > 5) {
        periodStarts.push(menstrualDays[i]);
      }
    }

    // Calculate cycle lengths (days between consecutive period starts)
    const cycleLengths = [];
    for (let i = 1; i < periodStarts.length; i++) {
      cycleLengths.push(differenceInDays(periodStarts[i], periodStarts[i - 1]));
    }

    // Period durations
    const periodDurations = [];
    let currentStart = menstrualDays[0];
    let currentEnd = menstrualDays[0];
    for (let i = 1; i < menstrualDays.length; i++) {
      if (differenceInDays(menstrualDays[i], currentEnd) <= 2) {
        currentEnd = menstrualDays[i];
      } else {
        periodDurations.push(differenceInDays(currentEnd, currentStart) + 1);
        currentStart = menstrualDays[i];
        currentEnd = menstrualDays[i];
      }
    }
    periodDurations.push(differenceInDays(currentEnd, currentStart) + 1);

    const avg = (arr) => arr.length ? Math.round(arr.reduce((s, v) => s + v, 0) / arr.length) : 0;

    return {
      totalPeriods: periodStarts.length,
      avgCycleLength: avg(cycleLengths),
      avgPeriodDuration: avg(periodDurations),
      lastPeriod: periodStarts[periodStarts.length - 1],
    };
  }, [logs]);

  if (!stats) {
    return (
      <div className="bg-card rounded-xl border border-border p-4 text-center text-sm text-muted-foreground">
        Log at least 2 period days to see cycle statistics.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-3 gap-3">
      <div className="bg-card rounded-xl border border-border p-4">
        <div className="flex items-center gap-2 mb-2">
          <div className="w-7 h-7 rounded-lg bg-primary/10 flex items-center justify-center">
            <Calendar className="w-3.5 h-3.5 text-primary" />
          </div>
          <span className="text-xs font-medium text-muted-foreground">Avg Cycle</span>
        </div>
        <span className="text-2xl font-bold font-mono">{stats.avgCycleLength || "—"}</span>
        <span className="text-sm text-muted-foreground ml-1">days</span>
      </div>
      <div className="bg-card rounded-xl border border-border p-4">
        <div className="flex items-center gap-2 mb-2">
          <div className="w-7 h-7 rounded-lg bg-red-500/10 flex items-center justify-center">
            <Droplets className="w-3.5 h-3.5 text-red-500" />
          </div>
          <span className="text-xs font-medium text-muted-foreground">Avg Period</span>
        </div>
        <span className="text-2xl font-bold font-mono">{stats.avgPeriodDuration}</span>
        <span className="text-sm text-muted-foreground ml-1">days</span>
      </div>
      <div className="bg-card rounded-xl border border-border p-4">
        <div className="flex items-center gap-2 mb-2">
          <div className="w-7 h-7 rounded-lg bg-purple-500/10 flex items-center justify-center">
            <Activity className="w-3.5 h-3.5 text-purple-500" />
          </div>
          <span className="text-xs font-medium text-muted-foreground">Periods Logged</span>
        </div>
        <span className="text-2xl font-bold font-mono">{stats.totalPeriods}</span>
      </div>
    </div>
  );
}