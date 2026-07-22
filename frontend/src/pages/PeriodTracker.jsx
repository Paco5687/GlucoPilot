import { useState, useEffect, useCallback, useMemo } from "react";
import { base44 } from "@/api/base44Client";
import { useViewingData } from "@/hooks/useViewingData";
import { useAuth } from "@/lib/AuthContext";
import { format } from "date-fns";
import { Loader2, ChevronDown, PencilLine, Sparkles, Repeat } from "lucide-react";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import PeriodCalendar from "../components/period/PeriodCalendar";
import PeriodLogForm from "../components/period/PeriodLogForm";
import LivelyImport from "../components/period/LivelyImport";
import CycleStats from "../components/period/CycleStats";
import CycleHero, { computeCycleFacts } from "../components/period/CycleHero";
import TemperatureChart from "../components/period/TemperatureChart";
import ContradictionPanel from "../components/ContradictionPanel";

export default function PeriodTracker() {
  const [logs, setLogs] = useState([]);
  const [ouraDays, setOuraDays] = useState([]);
  const [loading, setLoading] = useState(true);
  const [inferring, setInferring] = useState(false);
  const [selectedDate, setSelectedDate] = useState(new Date());
  const [showManual, setShowManual] = useState(false);
  const { fetchEntity, isViewingShared, viewingEmail } = useViewingData();
  const { isAdmin } = useAuth();

  const load = useCallback(async () => {
    const [data, oura] = await Promise.all([
      fetchEntity("PeriodLog", "-date", 5000),
      fetchEntity("OuraDaily", "-date", 120),
    ]);
    setLogs(data);
    setOuraDays(oura);
    setLoading(false);
  }, [fetchEntity, isViewingShared, viewingEmail]);

  useEffect(() => { setLoading(true); load(); }, [load]);

  const selectedDateStr = selectedDate ? format(selectedDate, "yyyy-MM-dd") : null;
  const existingLog = logs.find((l) => l.date === selectedDateStr);
  const facts = useMemo(() => computeCycleFacts(logs), [logs]);

  const handleSave = async (data) => {
    if (existingLog) {
      await base44.entities.PeriodLog.update(existingLog.id, { ...data, source: "manual" });
    } else {
      await base44.entities.PeriodLog.create({ ...data, source: "manual" });
    }
    await load();
  };

  const handleDelete = async (log) => {
    await base44.entities.PeriodLog.delete(log.id);
    await load();
  };

  const reinfer = async () => {
    setInferring(true);
    try {
      const res = await base44.functions.invoke("inferCycles", {});
      if (res.data?.ok) {
        toast.success(`Inference updated — ${res.data.ovulations_detected} ovulations, ${res.data.menses_onsets_detected} cycles detected`);
      } else {
        toast.info(res.data?.message || "Inference did not run");
      }
      await load();
    } catch (err) {
      toast.error(err?.response?.data?.error || err.message);
    }
    setInferring(false);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-6 h-6 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold">Cycle</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Phases inferred automatically from Oura nightly temperature; anything logged by hand always takes priority.
          </p>
        </div>
        {isAdmin && (
          <Button variant="outline" size="sm" onClick={reinfer} disabled={inferring} className="gap-2">
            {inferring ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5" />}
            Re-run inference
          </Button>
        )}
      </div>

      <ContradictionPanel domains={["hormone_timing"]} title="Hormone timing contradictions" />

      {/* Data-first: status tiles + the temperature signal */}
      <CycleHero logs={logs} />
      <TemperatureChart ouraDays={ouraDays} logs={logs} />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-1">
          <PeriodCalendar logs={logs} selectedDate={selectedDate} onSelectDate={setSelectedDate} />
        </div>
        <div className="lg:col-span-2 space-y-4">
          {/* Detected cycle history */}
          <div className="bg-card rounded-xl border border-border p-4">
            <h3 className="text-sm font-semibold mb-2 flex items-center gap-2">
              <Repeat className="w-4 h-4 text-primary" /> Cycle history
            </h3>
            {facts.starts.length ? (
              <div className="flex flex-wrap gap-2">
                {facts.starts.slice(-12).map((s, i, arr) => {
                  const next = arr[i + 1];
                  const len = next ? Math.round((new Date(next) - new Date(s)) / 86400000) : null;
                  return (
                    <div key={s} className="px-3 py-1.5 rounded-lg bg-secondary text-xs">
                      <span className="font-medium">
                        {new Date(s + "T00:00:00").toLocaleDateString([], { month: "short", day: "numeric" })}
                      </span>
                      <span className="text-muted-foreground">{len ? ` · ${len}d` : " · current"}</span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">No cycle starts detected yet.</p>
            )}
          </div>
          <CycleStats logs={logs} />
        </div>
      </div>

      {/* Manual logging & import — available, not dominant */}
      {isAdmin && (
      <div className="bg-card rounded-xl border border-border">
        <button
          className="w-full flex items-center justify-between p-4 text-sm font-semibold hover:bg-accent/50 rounded-xl transition-colors"
          onClick={() => setShowManual((v) => !v)}
        >
          <span className="flex items-center gap-2">
            <PencilLine className="w-4 h-4 text-primary" />
            Log or edit a day · import from Lively
          </span>
          <ChevronDown className={`w-4 h-4 transition-transform ${showManual ? "rotate-180" : ""}`} />
        </button>
        {showManual && (
          <div className="p-4 pt-0 grid grid-cols-1 lg:grid-cols-2 gap-4">
            <PeriodLogForm date={selectedDate} existingLog={existingLog} onSave={handleSave} onDelete={handleDelete} />
            <LivelyImport onImportComplete={load} />
          </div>
        )}
      </div>
      )}
    </div>
  );
}
