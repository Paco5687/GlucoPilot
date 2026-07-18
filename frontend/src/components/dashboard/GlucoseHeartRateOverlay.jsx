import { useState, useEffect, useCallback, useMemo } from "react";
import { useViewingData } from "@/hooks/useViewingData";
import { Activity, Loader2 } from "lucide-react";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, ReferenceArea } from "recharts";

const RANGES = [
  { key: 6, label: "6h" },
  { key: 12, label: "12h" },
  { key: 24, label: "24h" },
];

// Two stacked charts sharing one time axis — HR spikes align vertically with
// glucose events. (Deliberately not a dual-y-axis chart.)
export default function GlucoseHeartRateOverlay() {
  const { fetchEntity } = useViewingData();
  const [hours, setHours] = useState(12);
  const [glucose, setGlucose] = useState([]);
  const [hr, setHr] = useState([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const since = new Date(Date.now() - hours * 3600000).toISOString();
    const [g, h] = await Promise.all([
      fetchEntity("GlucoseReading", "-timestamp", 5000, { timestamp: { $gte: since } }),
      fetchEntity("FitbitHeartRate", "-timestamp", 5000, { timestamp: { $gte: since } }),
    ]);
    setGlucose(g.map((r) => ({ t: new Date(r.timestamp).getTime(), value: r.value })).filter((p) => !Number.isNaN(p.t)).sort((a, b) => a.t - b.t));
    setHr(h.map((r) => ({ t: new Date(r.timestamp).getTime(), bpm: r.bpm })).filter((p) => !Number.isNaN(p.t)).sort((a, b) => a.t - b.t));
    setLoading(false);
  }, [fetchEntity, hours]);

  useEffect(() => { load(); }, [load]);

  const domain = useMemo(() => [Date.now() - hours * 3600000, Date.now()], [hours]);
  const fmtTime = (t) => new Date(t).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  const tooltipStyle = { backgroundColor: "hsl(var(--card))", border: "1px solid hsl(var(--border))", borderRadius: "8px", fontSize: "12px" };

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Activity className="w-4 h-4 text-primary" />
          <h3 className="text-sm font-semibold">Glucose × Heart Rate</h3>
        </div>
        <div className="flex rounded-lg border border-border overflow-hidden">
          {RANGES.map((r) => (
            <button key={r.key} onClick={() => setHours(r.key)}
              className={`text-xs px-2.5 py-1 ${hours === r.key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}>
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-56"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>
      ) : hr.length === 0 ? (
        <div className="h-40 flex items-center justify-center text-center">
          <p className="text-sm text-muted-foreground">No heart-rate data in this window yet — it fills in as the watch syncs (or run an HR backfill).</p>
        </div>
      ) : (
        <div className="space-y-1">
          {/* Glucose (top) */}
          <div className="h-32">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={glucose} margin={{ top: 4, right: 8, bottom: 0, left: -8 }} syncId="ghr">
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
                <ReferenceArea y1={70} y2={180} fill="#10b981" fillOpacity={0.06} />
                <XAxis type="number" dataKey="t" domain={domain} scale="time" hide />
                <YAxis dataKey="value" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} domain={[40, (max) => Math.max(200, Math.ceil((max + 20) / 20) * 20)]} width={34} />
                <Tooltip contentStyle={tooltipStyle} labelFormatter={fmtTime} formatter={(v) => [`${v} mg/dL`, "Glucose"]} />
                <Line type="monotone" dataKey="value" stroke="#3b82f6" strokeWidth={2} dot={false} isAnimationActive={false} name="Glucose" />
              </LineChart>
            </ResponsiveContainer>
          </div>
          {/* Heart rate (bottom, shared x) */}
          <div className="h-32">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={hr} margin={{ top: 4, right: 8, bottom: 0, left: -8 }} syncId="ghr">
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
                <XAxis type="number" dataKey="t" domain={domain} scale="time" tickFormatter={fmtTime} tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} minTickGap={50} />
                <YAxis dataKey="bpm" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} domain={[(min) => Math.max(30, Math.floor((min - 5) / 5) * 5), (max) => Math.ceil((max + 5) / 5) * 5]} width={34} />
                <Tooltip contentStyle={tooltipStyle} labelFormatter={fmtTime} formatter={(v) => [`${v} bpm`, "Heart rate"]} />
                <Line type="monotone" dataKey="bpm" stroke="#f43f5e" strokeWidth={2} dot={false} isAnimationActive={false} name="Heart rate" />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="flex items-center gap-4 text-[11px] text-muted-foreground pt-1">
            <span className="inline-flex items-center gap-1"><span className="w-2.5 h-0.5 bg-[#3b82f6] inline-block" /> Glucose (target band shaded)</span>
            <span className="inline-flex items-center gap-1"><span className="w-2.5 h-0.5 bg-[#f43f5e] inline-block" /> Heart rate</span>
          </div>
        </div>
      )}
    </div>
  );
}
