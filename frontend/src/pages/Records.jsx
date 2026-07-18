import { useState, useEffect, useMemo } from "react";
import { base44 } from "@/api/base44Client";
import { useAuth } from "@/lib/AuthContext";
import { Button } from "@/components/ui/button";
import SafetyBanner from "../components/SafetyBanner";
import RecordUploadQueue from "../components/records/RecordUploadQueue";
import {
  FolderHeart, Loader2, FileText, Trash2, ExternalLink, AlertTriangle, FlaskConical, RefreshCw,
} from "lucide-react";
import { toast } from "sonner";
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, ReferenceArea, CartesianGrid,
} from "recharts";

function LabTrend({ name, points }) {
  // One analyte per chart (small multiples) — single series, own axis.
  const data = points.map((p) => ({
    date: p.collected_date,
    value: p.value,
    flag: p.flag,
    unit: p.unit,
  }));
  const latest = points[points.length - 1];
  const refLow = points.find((p) => p.reference_low != null)?.reference_low;
  const refHigh = points.find((p) => p.reference_high != null)?.reference_high;
  const outOfRange = latest.flag && latest.flag !== "normal" && latest.flag !== "";

  const values = points.map((p) => p.value).concat(refLow ?? [], refHigh ?? []);
  const min = Math.min(...values), max = Math.max(...values);
  const pad = (max - min) * 0.15 || Math.abs(max) * 0.1 || 1;

  return (
    <div className="bg-card rounded-xl border border-border p-4">
      <div className="flex items-start justify-between gap-2 mb-1">
        <div>
          <h4 className="font-semibold text-sm">{name}</h4>
          <p className="text-xs text-muted-foreground">
            {points.length} result{points.length === 1 ? "" : "s"}
            {refLow != null && refHigh != null ? ` · ref ${refLow}–${refHigh} ${latest.unit || ""}` : ""}
          </p>
        </div>
        <div className="text-right">
          <div className="text-lg font-bold tabular-nums">
            {latest.value}
            <span className="text-xs font-normal text-muted-foreground ml-1">{latest.unit}</span>
          </div>
          {outOfRange && (
            <span className="text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-red-100 text-red-700 inline-flex items-center gap-1">
              <AlertTriangle className="w-3 h-3" /> {latest.flag}
            </span>
          )}
        </div>
      </div>
      {points.length >= 2 ? (
        <div className="h-28">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 6, right: 6, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
              {refLow != null && refHigh != null && (
                <ReferenceArea y1={refLow} y2={refHigh} fill="hsl(var(--primary))" fillOpacity={0.07} stroke="none" />
              )}
              <XAxis dataKey="date" tick={{ fontSize: 10 }} stroke="hsl(var(--muted-foreground))" tickLine={false} />
              <YAxis
                domain={[Math.floor((min - pad) * 100) / 100, Math.ceil((max + pad) * 100) / 100]}
                tick={{ fontSize: 10 }}
                stroke="hsl(var(--muted-foreground))"
                tickLine={false}
                width={44}
              />
              <Tooltip
                formatter={(v, _n, item) => [`${v} ${item?.payload?.unit || ""}${item?.payload?.flag && item.payload.flag !== "normal" ? ` (${item.payload.flag})` : ""}`, name]}
                labelFormatter={(l) => l}
                contentStyle={{ fontSize: 12, borderRadius: 8 }}
              />
              <Line
                type="monotone"
                dataKey="value"
                stroke="hsl(var(--primary))"
                strokeWidth={2}
                dot={({ cx, cy, payload, index }) => {
                  const bad = payload.flag && payload.flag !== "normal" && payload.flag !== "";
                  return (
                    <circle
                      key={index}
                      cx={cx}
                      cy={cy}
                      r={4}
                      fill={bad ? "#dc2626" : "hsl(var(--primary))"}
                      stroke="hsl(var(--card))"
                      strokeWidth={2}
                    />
                  );
                }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground italic">One result so far — a trend appears after the next upload.</p>
      )}
    </div>
  );
}

export default function Records() {
  const { isAdmin } = useAuth();
  const [records, setRecords] = useState([]);
  const [labs, setLabs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [reprocessingId, setReprocessingId] = useState(null);
  const [retryingAll, setRetryingAll] = useState(false);
  const failedCount = records.filter((r) => r.status === "failed").length;

  useEffect(() => {
    load();
  }, []);

  async function load() {
    setLoading(true);
    try {
      const [recs, labRows] = await Promise.all([
        base44.entities.MedicalRecord.list("-created_date", 200),
        base44.entities.LabResult.list("collected_date", 5000),
      ]);
      setRecords(recs);
      setLabs(labRows);
    } catch {
      // keep whatever we had
    }
    setLoading(false);
  }

  async function reprocessOne(rec) {
    const res = await fetch(`/api/records/${rec.id}/reprocess`, { method: "POST", credentials: "same-origin" });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error(data?.detail || `Reprocess failed (${res.status})`);
    return data;
  }

  async function handleReprocess(rec) {
    setReprocessingId(rec.id);
    try {
      const data = await reprocessOne(rec);
      toast.success(`Reprocessed "${rec.filename}" — ${data.lab_results} lab${data.lab_results === 1 ? "" : "s"}`);
      await load();
    } catch (err) {
      toast.error(err.message || "Reprocess failed");
    }
    setReprocessingId(null);
  }

  async function handleRetryAllFailed() {
    const failed = records.filter((r) => r.status === "failed");
    if (!failed.length) return;
    setRetryingAll(true);
    let ok = 0;
    for (const rec of failed) {
      setReprocessingId(rec.id);
      try {
        await reprocessOne(rec);
        ok += 1;
      } catch {
        // leave it failed; continue with the rest
      }
    }
    setReprocessingId(null);
    setRetryingAll(false);
    toast[ok === failed.length ? "success" : "info"](`Reprocessed ${ok}/${failed.length} failed document${failed.length === 1 ? "" : "s"}`);
    await load();
  }

  async function handleDelete(rec) {
    if (!window.confirm(`Delete "${rec.filename}" and its extracted lab results?`)) return;
    try {
      const res = await fetch(`/api/records/${rec.id}`, { method: "DELETE", credentials: "same-origin" });
      if (!res.ok) throw new Error("Delete failed");
      toast.success("Record deleted");
      await load();
    } catch (err) {
      toast.error(err.message);
    }
  }

  const trendsByCategory = useMemo(() => {
    const byTest = new Map();
    for (const lab of labs) {
      if (lab.value == null || !lab.test_name) continue;
      const key = lab.test_name;
      if (!byTest.has(key)) byTest.set(key, []);
      byTest.get(key).push(lab);
    }
    const categories = new Map();
    for (const [test, points] of byTest) {
      points.sort((a, b) => String(a.collected_date).localeCompare(String(b.collected_date)));
      const cat = points[points.length - 1].category || "Other";
      if (!categories.has(cat)) categories.set(cat, []);
      categories.get(cat).push([test, points]);
    }
    return [...categories.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [labs]);

  return (
    <div className="space-y-6">
      <SafetyBanner />

      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold">Health Records</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Upload lab reports and medical documents (PDF/photo). They're stored and analyzed entirely on this server —
            nothing leaves the machine when the local AI model is selected.
          </p>
        </div>
        <FolderHeart className="w-6 h-6 text-primary" />
      </div>

      {/* Upload queue (admin only) */}
      {isAdmin && <RecordUploadQueue onComplete={load} />}

      {/* Lab trends */}
      {trendsByCategory.length > 0 && (
        <div className="space-y-5">
          <h2 className="font-semibold text-base flex items-center gap-2">
            <FlaskConical className="w-4 h-4 text-primary" /> Lab trends
          </h2>
          {trendsByCategory.map(([category, tests]) => (
            <div key={category}>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-2">{category}</h3>
              <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-4">
                {tests.map(([test, points]) => (
                  <LabTrend key={test} name={test} points={points} />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Documents */}
      <div className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <h2 className="font-semibold text-base flex items-center gap-2">
            <FileText className="w-4 h-4 text-primary" /> Documents
          </h2>
          {isAdmin && failedCount > 0 && (
            <Button variant="outline" size="sm" onClick={handleRetryAllFailed} disabled={retryingAll} className="gap-1.5 text-xs">
              {retryingAll ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
              Retry all failed ({failedCount})
            </Button>
          )}
        </div>
        {loading ? (
          <div className="flex items-center gap-2 text-muted-foreground text-sm">
            <Loader2 className="w-4 h-4 animate-spin" /> Loading…
          </div>
        ) : records.length === 0 ? (
          <p className="text-sm text-muted-foreground">No documents yet.</p>
        ) : (
          records.map((rec) => (
            <div key={rec.id} className="bg-card rounded-xl border border-border p-4 flex items-start gap-3">
              <FileText className="w-5 h-5 text-muted-foreground flex-shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-medium text-sm">{rec.filename}</span>
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-muted text-muted-foreground font-medium">
                    {rec.doc_type || rec.status}
                  </span>
                  {rec.record_date && <span className="text-xs text-muted-foreground">{rec.record_date}</span>}
                  {rec.status === "failed" && (
                    <span className="text-[10px] px-2 py-0.5 rounded-full bg-red-100 text-red-700 font-medium">failed</span>
                  )}
                </div>
                {rec.summary && <p className="text-sm text-muted-foreground mt-1 leading-relaxed">{rec.summary}</p>}
                {rec.lab_count != null && (
                  <p className="text-xs text-muted-foreground mt-1">{rec.lab_count} lab results extracted</p>
                )}
              </div>
              <div className="flex items-center gap-1 flex-shrink-0">
                {isAdmin && rec.status === "failed" && (
                  <button
                    onClick={() => handleReprocess(rec)}
                    disabled={reprocessingId === rec.id}
                    className="p-2 rounded-lg hover:bg-accent text-primary disabled:opacity-50"
                    title="Retry extraction"
                  >
                    <RefreshCw className={`w-4 h-4 ${reprocessingId === rec.id ? "animate-spin" : ""}`} />
                  </button>
                )}
                <a href={`/api/records/file/${rec.id}`} target="_blank" rel="noreferrer" className="p-2 rounded-lg hover:bg-accent text-muted-foreground" title="Open original">
                  <ExternalLink className="w-4 h-4" />
                </a>
                <button onClick={() => handleDelete(rec)} className="p-2 rounded-lg hover:bg-accent text-destructive" title="Delete">
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
