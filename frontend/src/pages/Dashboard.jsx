import { useState, useEffect, useCallback, useMemo } from "react";
import { base44 } from "@/api/base44Client";
import { useViewingData } from "@/hooks/useViewingData";
import CurrentGlucose from "../components/dashboard/CurrentGlucose";
import MetricsCards from "../components/dashboard/MetricsCards";
import GlucoseChart from "../components/dashboard/GlucoseChart";
import TreatmentTimeline from "../components/dashboard/TreatmentTimeline";
import TreatmentSummaryCards from "../components/dashboard/TreatmentSummaryCards";
import DailyTIRChart from "../components/dashboard/DailyTIRChart";
import DailyAverageChart from "../components/dashboard/DailyAverageChart";
import HeatmapChart from "../components/dashboard/HeatmapChart";
import DayOfWeekChart from "../components/dashboard/DayOfWeekChart";
import OuraPanel from "../components/dashboard/OuraPanel";
import WearablesPanel from "../components/dashboard/WearablesPanel";
import LiveHeartRate from "../components/dashboard/LiveHeartRate";
import FingerstickLogger from "../components/dashboard/FingerstickLogger";
import GlucoseOuraOverlay from "../components/dashboard/GlucoseOuraOverlay";
import CorrelationCards from "../components/dashboard/CorrelationCards";
import TimeRangePicker, { RANGES } from "../components/dashboard/TimeRangePicker";
import { Loader2 } from "lucide-react";


export default function Dashboard() {
  const [readings, setReadings] = useState([]);
  const [treatments, setTreatments] = useState([]);
  const [periodLogs, setPeriodLogs] = useState([]);
  const [ouraData, setOuraData] = useState([]);
  const [ouraHR, setOuraHR] = useState([]);
  const [wearables, setWearables] = useState([]);
  const [fingersticks, setFingersticks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [range, setRange] = useState("3h");
  const [customRange, setCustomRange] = useState(null);
  const [customReadings, setCustomReadings] = useState([]);
  const [customTreatments, setCustomTreatments] = useState([]);
  const [customLoading, setCustomLoading] = useState(false);
  const { fetchEntity, isViewingShared, viewingEmail } = useViewingData();

  const loadOura = useCallback(async () => {
    const o = await fetchEntity("OuraDaily", "-date", 90);
    setOuraData(o);
  }, [isViewingShared, viewingEmail]);

  const loadWearables = useCallback(async () => {
    const w = await fetchEntity("FitbitDaily", "-date", 120, { source: "google_health" });
    setWearables(w);
  }, [isViewingShared, viewingEmail]);

  const loadFingersticks = useCallback(async () => {
    try {
      const res = await base44.functions.invoke("fingerstick", { action: "list", days: 120 });
      setFingersticks(res.data?.readings || []);
    } catch {
      setFingersticks([]);
    }
  }, []);

  const loadOuraHR = useCallback(async () => {
    // Only fetch HR data for the selected time range to avoid huge payloads
    const rangeConfig = RANGES.find((r) => r.key === range);
    const hours = range === "custom" ? null : (rangeConfig?.hours || 24);
    const limit = hours && hours <= 24 ? 2000 : 5000;
    
    let filter = {};
    if (range === "custom" && customRange?.from && customRange?.to) {
      filter = {
        timestamp: {
          $gte: new Date(customRange.from).toISOString(),
          $lte: new Date(new Date(customRange.to).setHours(23, 59, 59, 999)).toISOString(),
        },
      };
    } else if (hours) {
      filter = { timestamp: { $gte: new Date(Date.now() - hours * 3600000).toISOString() } };
    }

    let hr = await fetchEntity("OuraHeartRate", "-timestamp", limit, filter);
    if (!hr.length && range !== "custom") {
      // Ring hasn't uploaded recently — show the latest available stretch
      // instead of an empty chart (the chart labels the staleness).
      hr = await fetchEntity("OuraHeartRate", "-timestamp", 360);
    }
    setOuraHR(hr);
  }, [isViewingShared, viewingEmail, range, customRange]);

  const load = useCallback(async () => {
    const [r, t, p] = await Promise.all([
      fetchEntity("GlucoseReading", "-timestamp", 26000),
      fetchEntity("Treatment", "-timestamp", 5000),
      fetchEntity("PeriodLog", "-date", 500),
      loadOura(),
      loadWearables(),
      loadFingersticks(),
    ]);
    setReadings(r);
    setTreatments(t);
    setPeriodLogs(p);
    setLoading(false);
  }, [isViewingShared, viewingEmail, loadOura, loadWearables, loadFingersticks]);

  useEffect(() => {
    setLoading(true);
    load();

    // Only subscribe to real-time updates for own data
    if (!isViewingShared) {
      const unsubReadings = base44.entities.GlucoseReading.subscribe(() => load());
      const unsubTreatments = base44.entities.Treatment.subscribe(() => load());
      const poll = setInterval(() => load(), 60 * 1000);
      return () => {
        unsubReadings();
        unsubTreatments();
        clearInterval(poll);
      };
    } else {
      const poll = setInterval(() => load(), 60 * 1000);
      return () => clearInterval(poll);
    }
  }, [load, isViewingShared]);

  // Fetch data on-demand for custom date ranges
  useEffect(() => {
    if (range !== "custom" || !customRange?.from || !customRange?.to) return;
    const fromISO = new Date(customRange.from).toISOString();
    const toISO = new Date(new Date(customRange.to).setHours(23, 59, 59, 999)).toISOString();

    setCustomLoading(true);
    Promise.all([
      fetchEntity("GlucoseReading", "-timestamp", 5000, { timestamp: { $gte: fromISO, $lte: toISO } }),
      fetchEntity("Treatment", "-timestamp", 2000, { timestamp: { $gte: fromISO, $lte: toISO } }),
    ]).then(([r, t]) => {
      setCustomReadings(r);
      setCustomTreatments(t);
      setCustomLoading(false);
    });
  }, [range, customRange, isViewingShared, viewingEmail]);

  const filteredReadings = useMemo(() => {
    if (range === "custom") return customReadings;
    const rangeConfig = RANGES.find((r) => r.key === range);
    if (!rangeConfig?.hours) return readings;
    const cutoff = Date.now() - rangeConfig.hours * 3600000;
    return readings.filter((r) => new Date(r.timestamp).getTime() >= cutoff);
  }, [readings, range, customReadings]);

  const filteredTreatments = useMemo(() => {
    if (range === "custom") return customTreatments;
    const rangeConfig = RANGES.find((r) => r.key === range);
    if (!rangeConfig?.hours) return treatments;
    const cutoff = Date.now() - rangeConfig.hours * 3600000;
    return treatments.filter((t) => new Date(t.timestamp).getTime() >= cutoff);
  }, [treatments, range, customTreatments]);

  const filteredOura = useMemo(() => {
    if (!ouraData.length) return [];
    if (range === "custom" && customRange?.from && customRange?.to) {
      const from = new Date(customRange.from).toISOString().split("T")[0];
      const to = new Date(customRange.to).toISOString().split("T")[0];
      return ouraData.filter((d) => d.date >= from && d.date <= to);
    }
    const rangeConfig = RANGES.find((r) => r.key === range);
    if (!rangeConfig?.hours) return ouraData;
    // OuraDaily is one record per day — hour-scale glucose ranges would leave
    // these charts nearly empty, so floor the Oura window at 14 days.
    const hours = Math.max(rangeConfig.hours, 14 * 24);
    const cutoffDate = new Date(Date.now() - hours * 3600000).toISOString().split("T")[0];
    return ouraData.filter((d) => d.date >= cutoffDate);
  }, [ouraData, range, customRange]);

  // Load HR data when range changes (already filtered server-side)
  useEffect(() => {
    if (!loading) loadOuraHR();
  }, [loadOuraHR, loading]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-6 h-6 animate-spin text-primary" />
      </div>
    );
  }

  const latest = readings[0];
  const previous = readings[1];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Dashboard</h1>
        <TimeRangePicker value={range} onChange={setRange} customRange={customRange} onCustomRangeChange={setCustomRange} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-1">
          <CurrentGlucose reading={latest} previousReading={previous} />
        </div>
        <div className="lg:col-span-2">
          <MetricsCards readings={filteredReadings.length ? filteredReadings : readings.slice(0, 288)} />
        </div>
      </div>

      {customLoading && (
        <div className="flex items-center justify-center h-16">
          <Loader2 className="w-5 h-5 animate-spin text-primary mr-2" />
          <span className="text-sm text-muted-foreground">Loading data for selected dates…</span>
        </div>
      )}

      <GlucoseChart readings={filteredReadings.length ? filteredReadings : readings.slice(0, 288)} treatments={filteredTreatments} periodLogs={periodLogs} fingersticks={fingersticks} />

      {wearables.length > 0 && <LiveHeartRate />}

      {!isViewingShared && <FingerstickLogger onAdded={loadFingersticks} />}

      <TreatmentTimeline treatments={filteredTreatments} />

      <TreatmentSummaryCards treatments={filteredTreatments} />

      <OuraPanel data={filteredOura} heartRateData={ouraHR} isViewingShared={isViewingShared} onRefresh={loadOura} />

      <WearablesPanel data={wearables} isViewingShared={isViewingShared} onRefresh={loadWearables} />

      {filteredOura.length > 0 && (
        <>
          <GlucoseOuraOverlay readings={filteredReadings.length ? filteredReadings : readings.slice(0, 288)} ouraData={filteredOura} />
          <CorrelationCards readings={filteredReadings.length ? filteredReadings : readings.slice(0, 288)} ouraData={filteredOura} />
        </>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <DailyTIRChart readings={filteredReadings} />
        <DailyAverageChart readings={filteredReadings} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <HeatmapChart readings={filteredReadings} />
        <DayOfWeekChart readings={filteredReadings} />
      </div>
    </div>
  );
}