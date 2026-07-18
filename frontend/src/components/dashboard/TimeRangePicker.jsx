import { useState } from "react";
import { cn } from "@/lib/utils";
import { Calendar as CalendarIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Calendar } from "@/components/ui/calendar";
import { format } from "date-fns";

const RANGES = [
  { key: "3h", label: "3H", hours: 3 },
  { key: "6h", label: "6H", hours: 6 },
  { key: "12h", label: "12H", hours: 12 },
  { key: "24h", label: "24H", hours: 24 },
  { key: "3d", label: "3D", hours: 72 },
  { key: "7d", label: "7D", hours: 168 },
  { key: "14d", label: "14D", hours: 336 },
  { key: "30d", label: "30D", hours: 720 },
  { key: "60d", label: "60D", hours: 1440 },
  { key: "90d", label: "90D", hours: 2160 },
];

export { RANGES };

export default function TimeRangePicker({ value, onChange, customRange, onCustomRangeChange }) {
  const [open, setOpen] = useState(false);
  const [selecting, setSelecting] = useState(null); // {from, to} during selection

  const isCustom = value === "custom";

  const handleSelect = (range) => {
    if (!range) return;
    setSelecting(range);
    // If both from and to are selected, apply
    if (range.from && range.to) {
      onCustomRangeChange?.({ from: range.from, to: range.to });
      onChange("custom");
      setSelecting(null);
      setOpen(false);
    }
  };

  const customLabel = isCustom && customRange?.from && customRange?.to
    ? `${format(customRange.from, "MMM d")} – ${format(customRange.to, "MMM d")}`
    : "Custom";

  return (
    <div className="flex items-center gap-1 bg-secondary/50 rounded-lg p-1 flex-wrap">
      {RANGES.map((r) => (
        <button
          key={r.key}
          onClick={() => onChange(r.key)}
          className={cn(
            "px-2.5 py-1.5 rounded-md text-xs font-medium transition-all",
            value === r.key
              ? "bg-card text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground"
          )}
        >
          {r.label}
        </button>
      ))}
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button
            className={cn(
              "px-2.5 py-1.5 rounded-md text-xs font-medium transition-all flex items-center gap-1",
              isCustom
                ? "bg-card text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            )}
          >
            <CalendarIcon className="w-3 h-3" />
            {customLabel}
          </button>
        </PopoverTrigger>
        <PopoverContent className="w-auto p-0" align="end">
          <Calendar
            mode="range"
            selected={selecting || customRange}
            onSelect={handleSelect}
            numberOfMonths={1}
            disabled={{ after: new Date() }}
            initialFocus
          />
        </PopoverContent>
      </Popover>
    </div>
  );
}