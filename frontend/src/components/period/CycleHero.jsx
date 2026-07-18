import { useMemo } from "react";
import { Heart, CalendarClock, Repeat, Thermometer } from "lucide-react";

const PHASE_STYLES = {
  menstrual: { label: "Menstrual", chip: "bg-red-100 text-red-700" },
  follicular: { label: "Follicular", chip: "bg-sky-100 text-sky-700" },
  ovulation: { label: "Ovulation", chip: "bg-amber-100 text-amber-700" },
  luteal: { label: "Luteal", chip: "bg-violet-100 text-violet-700" },
};

export function computeCycleFacts(logs) {
  // menstrual starts = menstrual day with no menstrual day immediately before
  const byDate = new Map(logs.map((l) => [l.date, l]));
  const starts = [];
  for (const log of logs) {
    if (log.phase !== "menstrual") continue;
    const prev = new Date(log.date + "T00:00:00");
    prev.setDate(prev.getDate() - 1);
    const prevKey = prev.toISOString().slice(0, 10);
    if (byDate.get(prevKey)?.phase !== "menstrual") starts.push(log.date);
  }
  starts.sort();
  const lengths = [];
  for (let i = 1; i < starts.length; i++) {
    const d = Math.round((new Date(starts[i]) - new Date(starts[i - 1])) / 86400000);
    if (d >= 15 && d <= 60) lengths.push(d);
  }
  const avgLen = lengths.length ? lengths.reduce((a, b) => a + b, 0) / lengths.length : null;

  const today = new Date().toISOString().slice(0, 10);
  const todayLog = byDate.get(today);
  // fall back to the most recent logged phase within 3 days
  let currentPhase = todayLog?.phase || null;
  if (!currentPhase) {
    for (let back = 1; back <= 3 && !currentPhase; back++) {
      const d = new Date(today + "T00:00:00");
      d.setDate(d.getDate() - back);
      currentPhase = byDate.get(d.toISOString().slice(0, 10))?.phase || null;
    }
  }

  const lastStart = starts[starts.length - 1] || null;
  const cycleDay = lastStart ? Math.round((new Date(today) - new Date(lastStart)) / 86400000) + 1 : null;
  const nextPredicted =
    lastStart && avgLen
      ? new Date(new Date(lastStart).getTime() + Math.round(avgLen) * 86400000).toISOString().slice(0, 10)
      : null;

  return { starts, lengths, avgLen, currentPhase, cycleDay, lastStart, nextPredicted };
}

export default function CycleHero({ logs }) {
  const facts = useMemo(() => computeCycleFacts(logs), [logs]);
  const style = PHASE_STYLES[facts.currentPhase] || null;
  const daysToNext = facts.nextPredicted
    ? Math.round((new Date(facts.nextPredicted) - new Date(new Date().toISOString().slice(0, 10))) / 86400000)
    : null;

  const tiles = [
    {
      icon: Heart,
      label: "Current phase",
      value: style ? style.label : "—",
      sub: facts.cycleDay ? `Cycle day ${facts.cycleDay}` : "no data yet",
      chip: style?.chip,
    },
    {
      icon: CalendarClock,
      label: "Next period (est.)",
      value: facts.nextPredicted
        ? new Date(facts.nextPredicted + "T00:00:00").toLocaleDateString([], { month: "short", day: "numeric" })
        : "—",
      sub: daysToNext != null ? (daysToNext >= 0 ? `in ~${daysToNext} days` : `${-daysToNext} days ago`) : "",
    },
    {
      icon: Repeat,
      label: "Average cycle",
      value: facts.avgLen ? `${facts.avgLen.toFixed(1)} days` : "—",
      sub: `${facts.lengths.length} measured cycle${facts.lengths.length === 1 ? "" : "s"}`,
    },
    {
      icon: Thermometer,
      label: "Data source",
      value: "Oura temperature",
      sub: "auto-inferred nightly",
    },
  ];

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      {tiles.map((t) => {
        const Icon = t.icon;
        return (
          <div key={t.label} className="bg-card rounded-xl border border-border p-4">
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground mb-1">
              <Icon className="w-3.5 h-3.5" /> {t.label}
            </div>
            <div className="flex items-center gap-2">
              <span className="text-lg font-bold">{t.value}</span>
              {t.chip && <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${t.chip}`}>now</span>}
            </div>
            {t.sub && <p className="text-xs text-muted-foreground mt-0.5">{t.sub}</p>}
          </div>
        );
      })}
    </div>
  );
}
