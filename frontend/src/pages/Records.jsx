import { useState, useEffect, useMemo, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import { base44 } from "@/api/base44Client";
import { useAuth } from "@/lib/AuthContext";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import SafetyBanner from "../components/SafetyBanner";
import RecordUploadQueue from "../components/records/RecordUploadQueue";
import LabsView from "../components/records/LabsView";
import MedicalRecordReview from "../components/records/MedicalRecordReview";
import ContradictionPanel from "../components/ContradictionPanel";
import { FolderHeart, Loader2, FileText, FlaskConical, Trash2, ExternalLink, RefreshCw } from "lucide-react";
import { toast } from "sonner";

const TABS = ["labs", "documents"];

// The same rows LabsView charts: a value, a name, and not rejected in review.
const isShownLab = (lab) => lab.value != null && lab.test_name && lab.verification_status !== "rejected";

// A count badge for a tab. On phones only the number shows, so both tabs fit on
// one line; the label stays readable to screen readers and in the tooltip.
function Pill({ tone, count, label }) {
  const cls = tone === "red" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700";
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium tabular-nums ${cls}`} title={`${count} ${label}`}>
      {count}<span className="sr-only sm:not-sr-only"> {label}</span>
    </span>
  );
}

function Loading() {
  return (
    <div className="flex items-center gap-2 text-muted-foreground text-sm py-6">
      <Loader2 className="w-4 h-4 animate-spin" /> Loading…
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
  const [searchParams, setSearchParams] = useSearchParams();
  // Without ?tab=, open on lab results when there are any, else on documents.
  // Decided once, so a reload after an upload never switches tabs under the user.
  const [defaultTab, setDefaultTab] = useState("labs");
  const defaultChosen = useRef(false);

  const failedCount = records.filter((r) => r.status === "failed").length;
  const reviewCount = records.filter((r) => r.status === "needs_review").length;
  const labCount = useMemo(() => labs.filter(isShownLab).length, [labs]);
  const requestedTab = searchParams.get("tab");
  const tab = TABS.includes(requestedTab) ? requestedTab : defaultTab;

  function selectTab(next) {
    setSearchParams((prev) => {
      const params = new URLSearchParams(prev);
      params.set("tab", next);
      return params;
    }, { replace: true });
  }

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
      if (!defaultChosen.current) {
        defaultChosen.current = true;
        setDefaultTab(labRows.some(isShownLab) ? "labs" : "documents");
      }
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

  const firstLoad = loading && !records.length && !labs.length;

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

      {/* Stays above the tabs: open contradictions are shown whichever tab is selected. */}
      <ContradictionPanel domains={["labs", "hormone_timing", "source_revision"]} title="Record and lab contradictions" />

      <Tabs value={tab} onValueChange={selectTab}>
        <TabsList className="h-auto max-w-full overflow-x-auto justify-start">
          <TabsTrigger value="labs" className="gap-2 px-3 py-1.5">
            <FlaskConical className="w-4 h-4 hidden sm:block" /> Lab results
            {!firstLoad && <span className="text-xs tabular-nums text-muted-foreground">{labCount}</span>}
          </TabsTrigger>
          <TabsTrigger value="documents" className="gap-2 px-3 py-1.5">
            <FileText className="w-4 h-4 hidden sm:block" /> Documents
            {!firstLoad && <span className="text-xs tabular-nums text-muted-foreground">{records.length}</span>}
            {failedCount > 0 && <Pill tone="red" count={failedCount} label="failed" />}
            {reviewCount > 0 && <Pill tone="amber" count={reviewCount} label="to review" />}
          </TabsTrigger>
        </TabsList>

        {/* Both panels stay mounted so an upload in progress, lab filters and open
            charts survive switching tabs. */}
        <TabsContent value="labs" forceMount className="mt-4 data-[state=inactive]:hidden">
          {firstLoad ? (
            <Loading />
          ) : labCount === 0 ? (
            <div className="bg-card rounded-xl border border-dashed border-border p-8 text-center">
              <FlaskConical className="w-6 h-6 mx-auto text-muted-foreground" />
              <p className="text-sm font-medium mt-2">No lab results yet</p>
              <p className="text-xs text-muted-foreground mt-1">Lab values appear here after a lab report is uploaded and extracted.</p>
              {isAdmin && (
                <Button variant="outline" size="sm" className="mt-3" onClick={() => selectTab("documents")}>
                  Upload a lab report
                </Button>
              )}
            </div>
          ) : (
            <LabsView labs={labs} />
          )}
        </TabsContent>

        <TabsContent value="documents" forceMount className="mt-4 space-y-3 data-[state=inactive]:hidden">
          {isAdmin && <RecordUploadQueue onComplete={load} />}

          {records.length > 0 && (
            <div className="flex items-center justify-between gap-3 min-h-8">
              <p className="text-xs text-muted-foreground">Newest first, by the date each document was taken.</p>
              {isAdmin && failedCount > 0 && (
                <Button variant="outline" size="sm" onClick={handleRetryAllFailed} disabled={retryingAll} className="gap-1.5 text-xs">
                  {retryingAll ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                  Retry all failed ({failedCount})
                </Button>
              )}
            </div>
          )}

          {firstLoad ? (
            <Loading />
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
                    {rec.status === "needs_review" && (
                      <span className="text-[10px] px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 font-medium">needs review</span>
                    )}
                  </div>
                  {rec.summary && <p className="text-sm text-muted-foreground mt-1 leading-relaxed">{rec.summary}</p>}
                  {rec.status === "needs_review" && rec.error && (
                    <p className="text-xs text-amber-700 mt-1">{rec.error}</p>
                  )}
                  {rec.lab_count != null && (
                    <p className="text-xs text-muted-foreground mt-1">{rec.lab_count} lab results extracted</p>
                  )}
                </div>
                <div className="flex items-center gap-1 flex-shrink-0">
                  {(rec.status === "processed" || rec.status === "needs_review") && (
                    <MedicalRecordReview record={rec} isAdmin={isAdmin} onChanged={load} />
                  )}
                  {isAdmin && (
                    <button
                      onClick={() => handleReprocess(rec)}
                      disabled={reprocessingId === rec.id}
                      className="p-2 rounded-lg hover:bg-accent text-primary disabled:opacity-50"
                      title={rec.status === "failed" ? "Retry extraction" : "Reprocess document"}
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
        </TabsContent>
      </Tabs>
    </div>
  );
}
