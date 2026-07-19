import { useState, useEffect } from "react";
import { base44 } from "@/api/base44Client";
import { useAuth } from "@/lib/AuthContext";
import { Button } from "@/components/ui/button";
import SafetyBanner from "../components/SafetyBanner";
import RecordUploadQueue from "../components/records/RecordUploadQueue";
import LabsView from "../components/records/LabsView";
import { FolderHeart, Loader2, FileText, Trash2, ExternalLink, RefreshCw } from "lucide-react";
import { toast } from "sonner";

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
      // Organize by date taken (record_date), not upload date; undated fall back
      // to upload date and sink to the bottom.
      recs.sort((a, b) =>
        String(b.record_date || b.created_date || "").localeCompare(String(a.record_date || a.created_date || ""))
      );
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

      {/* Lab trends — switchable Index / Charts / Matrix views with filters */}
      <LabsView labs={labs} />

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
                  <span className="font-medium text-sm" title={rec.filename}>{rec.title || rec.filename}</span>
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-muted text-muted-foreground font-medium">
                    {rec.doc_type || rec.status}
                  </span>
                  {rec.record_date && <span className="text-xs text-muted-foreground">taken {rec.record_date}</span>}
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
