import { Calendar } from "@/components/ui/calendar";
import { parseISO } from "date-fns";

const PHASE_COLORS = {
  menstrual: "bg-red-500",
  follicular: "bg-pink-400",
  ovulation: "bg-purple-500",
  luteal: "bg-amber-500",
};

const FLOW_OPACITY = {
  none: "opacity-20",
  spotting: "opacity-40",
  light: "opacity-60",
  medium: "opacity-80",
  heavy: "opacity-100",
};

export default function PeriodCalendar({ logs, selectedDate, onSelectDate }) {
  const logsByDate = {};
  logs.forEach((l) => {
    logsByDate[l.date] = l;
  });

  const modifiers = {
    menstrual: [],
    follicular: [],
    ovulation: [],
    luteal: [],
    hasFlow: [],
  };

  logs.forEach((l) => {
    const d = parseISO(l.date);
    if (l.phase && modifiers[l.phase]) modifiers[l.phase].push(d);
    if (l.flow && l.flow !== "none") modifiers.hasFlow.push(d);
  });

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <Calendar
        mode="single"
        selected={selectedDate}
        onSelect={onSelectDate}
        disabled={{ after: new Date() }}
        modifiers={modifiers}
        modifiersStyles={{
          menstrual: { backgroundColor: "hsl(0, 72%, 51%, 0.15)", borderRadius: "50%" },
          follicular: { backgroundColor: "hsl(330, 70%, 60%, 0.15)", borderRadius: "50%" },
          ovulation: { backgroundColor: "hsl(270, 60%, 50%, 0.15)", borderRadius: "50%" },
          luteal: { backgroundColor: "hsl(38, 92%, 50%, 0.15)", borderRadius: "50%" },
        }}
        className="w-full"
      />
      <div className="flex flex-wrap gap-3 mt-3 text-xs text-muted-foreground justify-center">
        <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full bg-red-500/40" /> Menstrual</span>
        <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full bg-pink-400/40" /> Follicular</span>
        <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full bg-purple-500/40" /> Ovulation</span>
        <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full bg-amber-500/40" /> Luteal</span>
      </div>
    </div>
  );
}