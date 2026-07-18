import { useState } from "react";
import { Link } from "react-router-dom";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Loader2, RefreshCw, Heart, Moon, Droplets, Wind, Footprints, Flame, ArrowRight, ArrowUp, ArrowDown } from "lucide-react";

function Tile({ icon: Icon, label, value, unit, detail, color, bg, delta }) {
  if (value == null) return null;
  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center gap-2 mb-2">
        <div className={`w-8 h-8 rounded-lg ${bg} flex items-center justify-center`}>
          <Icon className={`w-4 h-4 ${color}`} />
        </div>
        <span className="text-xs text-muted-foreground">{label}</span>
      </div>
      <div className="flex items-baseline gap-1">
        <span className="text-2xl font-semibold tabular-nums">{value}</span>
        {unit && <span className="text-xs text-muted-foreground">{unit}</span>}
        {delta != null && delta !== 0 && (
          <span className={`ml-1 text-[11px] inline-flex items-center ${delta > 0 ? "text-emerald-500" : "text-rose-500"}`}>
            {delta > 0 ? <ArrowUp className="w-3 h-3" /> : <ArrowDown className="w-3 h-3" />}
            {Math.abs(delta)}
          </span>
        )}
      </div>
      {detail && <p className="text-[11px] text-muted-foreground mt-0.5">{detail}</p>}
    </div>
  );
}

export default function WearablesPanel({ data, isViewingShared, onRefresh }) {
  const [syncing, setSyncing] = useState(false);

  async function handleSync() {
    setSyncing(true);
    try {
      await base44.functions.invoke("googleHealth", { action: "sync", days: 30 });
      if (onRefresh) await onRefresh();
    } finally {
      setSyncing(false);
    }
  }

  if (!data || data.length === 0) return null;

  // data is newest-first; find the most recent non-null value + the one before it.
  const findLatest = (field) => {
    let current = null;
    for (let i = 0; i < data.length; i++) {
      if (data[i][field] != null) {
        if (current == null) current = { value: data[i][field], date: data[i].date };
        else return { ...current, prev: data[i][field] };
      }
    }
    return current ? { ...current, prev: null } : null;
  };
  const delta = (l, round = 0) =>
    l && l.prev != null ? Number((l.value - l.prev).toFixed(round)) : null;

  const rhr = findLatest("resting_heart_rate");
  const sleep = findLatest("sleep_minutes");
  const spo2 = findLatest("spo2_avg");
  const resp = findLatest("breathing_rate");
  const steps = findLatest("steps");
  const active = findLatest("active_minutes");
  const latestDate = data[0]?.date;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Heart className="w-4 h-4 text-sky-500" />
          <h2 className="text-sm font-semibold">Fitbit</h2>
          <span className="text-[10px] text-muted-foreground px-1.5 py-0.5 rounded bg-muted">via Google Health</span>
          {latestDate && <span className="text-xs text-muted-foreground">{latestDate}</span>}
        </div>
        <div className="flex items-center gap-2">
          <Link to="/wearables" className="text-xs text-primary inline-flex items-center gap-1 hover:underline">
            Trends <ArrowRight className="w-3 h-3" />
          </Link>
          {!isViewingShared && (
            <Button variant="outline" size="sm" onClick={handleSync} disabled={syncing} className="gap-1.5 text-xs">
              {syncing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
              {syncing ? "Syncing..." : "Sync"}
            </Button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
        <Tile icon={Heart} label="Resting HR" value={rhr?.value} unit="bpm" delta={delta(rhr)}
          color="text-rose-500" bg="bg-rose-500/10" />
        <Tile icon={Moon} label="Sleep" value={sleep ? (sleep.value / 60).toFixed(1) : null} unit="h"
          detail={sleep ? `${sleep.value} min` : null} color="text-indigo-500" bg="bg-indigo-500/10" />
        <Tile icon={Droplets} label="SpO₂" value={spo2?.value} unit="%" delta={delta(spo2, 1)}
          detail={spo2 ? "avg overnight" : null} color="text-sky-500" bg="bg-sky-500/10" />
        <Tile icon={Wind} label="Respiratory" value={resp?.value} unit="br/min" delta={delta(resp, 1)}
          color="text-teal-500" bg="bg-teal-500/10" />
        <Tile icon={Footprints} label="Steps" value={steps ? steps.value.toLocaleString() : null}
          color="text-orange-500" bg="bg-orange-500/10" />
        <Tile icon={Flame} label="Active" value={active?.value} unit="min" delta={delta(active)}
          color="text-emerald-500" bg="bg-emerald-500/10" />
      </div>
    </div>
  );
}
