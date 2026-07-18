import { useState, useEffect } from "react";
import { useViewingData } from "@/hooks/useViewingData";
import ComparisonPanel from "../components/compare/ComparisonPanel";
import ComparisonSummary from "../components/compare/ComparisonSummary";
import SafetyBanner from "../components/SafetyBanner";
import { GitCompare, Loader2 } from "lucide-react";
import { calculateTimeInRange, calculateAverage, calculateCV } from "@/lib/glucoseUtils";

const PERIODS = [
  { value: "7d", label: "Last 7 vs Prev 7 days", days: 7 },
  { value: "14d", label: "Last 14 vs Prev 14 days", days: 14 },
  { value: "30d", label: "Last 30 vs Prev 30 days", days: 30 },
  { value: "weekday", label: "Weekdays vs Weekends", days: 0 },
];

function computeMetrics(readings) {
  if (!readings.length) return { tir: 0, above: 0, below: 0, avg: 0, cv: 0, count: 0 };
  const tir = calculateTimeInRange(readings);
  return {
    tir: tir.inRange,
    above: tir.above,
    below: tir.below,
    avg: calculateAverage(readings),
    cv: calculateCV(readings),
    count: readings.length,
  };
}

export default function Compare() {
  const [readings, setReadings] = useState([]);
  const [loading, setLoading] = useState(true);
  const [period, setPeriod] = useState("7d");
  const { fetchEntity, isViewingShared, viewingEmail } = useViewingData();

  useEffect(() => {
    async function load() {
      setLoading(true);
      const batch1 = await fetchEntity("GlucoseReading", "-timestamp", 5000);
      let allData = batch1;
      if (batch1.length === 5000) {
        const batch2 = await fetchEntity("GlucoseReading", "-timestamp", 5000);
        allData = [...batch1, ...batch2];
      }
      setReadings(allData);
      setLoading(false);
    }
    load();
  }, [isViewingShared, viewingEmail]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-6 h-6 animate-spin text-primary" />
      </div>
    );
  }

  const now = Date.now();
  let current, previous, currentLabel, previousLabel;

  if (period === "weekday") {
    const weekdays = readings.filter((r) => {
      const d = new Date(r.timestamp).getDay();
      return d >= 1 && d <= 5;
    });
    const weekends = readings.filter((r) => {
      const d = new Date(r.timestamp).getDay();
      return d === 0 || d === 6;
    });
    current = computeMetrics(weekdays);
    previous = computeMetrics(weekends);
    currentLabel = "Weekdays";
    previousLabel = "Weekends";
  } else {
    const days = PERIODS.find((p) => p.value === period).days;
    const msPerDay = 86400000;
    const currentReadings = readings.filter(
      (r) => now - new Date(r.timestamp).getTime() < days * msPerDay
    );
    const previousReadings = readings.filter((r) => {
      const age = now - new Date(r.timestamp).getTime();
      return age >= days * msPerDay && age < days * 2 * msPerDay;
    });
    current = computeMetrics(currentReadings);
    previous = computeMetrics(previousReadings);
    currentLabel = `Last ${days} days`;
    previousLabel = `Previous ${days} days`;
  }

  return (
    <div className="space-y-6">
      <SafetyBanner />

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold">Comparison Engine</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Compare glucose metrics across different time periods
          </p>
        </div>
        <GitCompare className="w-6 h-6 text-primary" />
      </div>

      {/* Period selector */}
      <div className="flex flex-wrap gap-2">
        {PERIODS.map((p) => (
          <button
            key={p.value}
            onClick={() => setPeriod(p.value)}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${
              period === p.value
                ? "bg-primary text-primary-foreground"
                : "bg-secondary text-secondary-foreground hover:bg-accent"
            }`}
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* Reading counts */}
      <div className="flex items-center gap-4 text-xs text-muted-foreground">
        <span>{currentLabel}: <strong className="text-foreground">{current.count.toLocaleString()}</strong> readings</span>
        <span>{previousLabel}: <strong className="text-foreground">{previous.count.toLocaleString()}</strong> readings</span>
      </div>

      <ComparisonSummary
        current={current}
        previous={previous}
        currentLabel={currentLabel}
        previousLabel={previousLabel}
      />

      <ComparisonPanel
        current={current}
        previous={previous}
        currentLabel={currentLabel}
        previousLabel={previousLabel}
      />
    </div>
  );
}