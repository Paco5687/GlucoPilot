import { useState } from "react";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Loader2, RefreshCw, Moon, Flame, Activity } from "lucide-react";
import OuraScoreCard from "./OuraScoreCard";
import OuraScoresChart from "./OuraScoresChart";

// Oura's role on the dashboard is its unique derived scores (sleep quality,
// readiness, activity) and cycle inference. Objective physiology — heart rate,
// HRV, SpO₂, steps, sleep duration — comes from Fitbit / Google Health (the more
// live source), so those live in WearablesPanel and the Live Heart Rate chart
// instead of being duplicated here.
export default function OuraPanel({ data, isViewingShared, onRefresh }) {
  const [syncing, setSyncing] = useState(false);

  async function handleSync() {
    setSyncing(true);
    await base44.functions.invoke("ouraSync", { days: 30 });
    if (onRefresh) await onRefresh();
    setSyncing(false);
  }

  if (!data || data.length === 0) return null;

  // Find the most recent day with data for each metric (today may not have scores yet)
  const findLatest = (field) => {
    for (let i = 0; i < data.length; i++) {
      if (data[i][field] != null) return { current: data[i], prev: data.find((d, j) => j > i && d[field] != null) };
    }
    return { current: null, prev: null };
  };
  const sleep = findLatest("sleep_score");
  const readiness = findLatest("readiness_score");
  const activity = findLatest("activity_score");
  const latestDate = data[0]?.date;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-lg">💍</span>
          <h2 className="text-sm font-semibold">Oura Ring</h2>
          <span className="text-[10px] text-muted-foreground px-1.5 py-0.5 rounded bg-muted">scores &amp; readiness</span>
          {latestDate && <span className="text-xs text-muted-foreground">{latestDate}</span>}
        </div>
        {!isViewingShared && (
          <Button variant="outline" size="sm" onClick={handleSync} disabled={syncing} className="gap-1.5 text-xs">
            {syncing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
            {syncing ? "Syncing..." : "Sync"}
          </Button>
        )}
      </div>

      <div className="grid grid-cols-3 gap-3">
        <OuraScoreCard
          icon={Moon}
          label="Sleep"
          score={sleep.current?.sleep_score}
          prevScore={sleep.prev?.sleep_score}
          color="text-indigo-500"
          bgColor="bg-indigo-500/10"
        />
        <OuraScoreCard
          icon={Activity}
          label="Readiness"
          score={readiness.current?.readiness_score}
          prevScore={readiness.prev?.readiness_score}
          color="text-emerald-500"
          bgColor="bg-emerald-500/10"
        />
        <OuraScoreCard
          icon={Flame}
          label="Activity"
          score={activity.current?.activity_score}
          prevScore={activity.prev?.activity_score}
          color="text-orange-500"
          bgColor="bg-orange-500/10"
        />
      </div>
      <OuraScoresChart data={data} />
    </div>
  );
}
