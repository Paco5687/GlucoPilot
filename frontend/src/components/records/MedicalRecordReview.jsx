import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, ExternalLink, FileSearch, Loader2, Pencil, XCircle } from "lucide-react";
import { toast } from "sonner";

const STATUS_TONE = {
  approved: "bg-emerald-100 text-emerald-800",
  edited: "bg-blue-100 text-blue-800",
  rejected: "bg-rose-100 text-rose-800",
  unverified: "bg-amber-100 text-amber-800",
};

function shownValue(observation, mode) {
  if (mode === "original") {
    return {
      name: observation.original_name,
      value: observation.original_value,
      unit: observation.original_unit,
      range: observation.original_reference_range,
      flag: observation.original_flag,
      date: observation.original_collected_date,
    };
  }
  return {
    name: observation.normalized_name,
    value: observation.value_kind === "numeric" ? observation.normalized_value : observation.original_value,
    unit: observation.normalized_unit,
    range: observation.reference_low != null || observation.reference_high != null
      ? `${observation.reference_low ?? ""}–${observation.reference_high ?? ""}`
      : "",
    flag: observation.normalized_flag,
    date: observation.normalized_collected_date,
  };
}

function initialEdit(observation) {
  return {
    test_name: observation.normalized_name || "",
    value: observation.value_kind === "numeric" ? String(observation.normalized_value ?? "") : observation.original_value || "",
    value_kind: observation.value_kind || "numeric",
    unit: observation.normalized_unit || "",
    reference_low: observation.reference_low == null ? "" : String(observation.reference_low),
    reference_high: observation.reference_high == null ? "" : String(observation.reference_high),
    flag: observation.normalized_flag || "",
    specimen: observation.specimen || "",
    collected_date: observation.normalized_collected_date || "",
    category: observation.category || "",
  };
}

export default function MedicalRecordReview({ record, isAdmin, onChanged }) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [mode, setMode] = useState("normalized");
  const [editing, setEditing] = useState(false);
  const [edit, setEdit] = useState(() => initialEdit({}));
  const [saving, setSaving] = useState(false);

  const selected = useMemo(
    () => data?.observations?.find((item) => item.id === selectedId) || data?.observations?.[0] || null,
    [data, selectedId]
  );

  useEffect(() => {
    if (!selected) return;
    setSelectedId(selected.id);
    setEdit(initialEdit(selected));
    setEditing(false);
  }, [selected?.id]);

  async function load() {
    setLoading(true);
    try {
      const response = await fetch(`/api/records/${record.id}/extractions`, { credentials: "same-origin" });
      const body = await response.json().catch(() => null);
      if (!response.ok) throw new Error(body?.detail || "Could not load extraction review");
      setData(body);
      setSelectedId((current) => body.observations?.some((item) => item.id === current) ? current : body.observations?.[0]?.id || null);
    } catch (error) {
      toast.error(error.message || "Could not load extraction review");
    } finally {
      setLoading(false);
    }
  }

  async function setDialog(next) {
    setOpen(next);
    if (next) await load();
  }

  async function review(action) {
    if (!selected) return;
    setSaving(true);
    try {
      const patch = action === "edit" ? {
        ...edit,
        value: edit.value_kind === "numeric" && edit.value !== "" ? Number(edit.value) : edit.value,
        reference_low: edit.reference_low === "" ? null : Number(edit.reference_low),
        reference_high: edit.reference_high === "" ? null : Number(edit.reference_high),
      } : undefined;
      const response = await fetch(`/api/records/${record.id}/extractions/${selected.id}/verify`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, patch }),
      });
      const body = await response.json().catch(() => null);
      if (!response.ok) throw new Error(body?.detail || `Could not ${action} result`);
      toast.success(action === "edit" ? "Correction saved" : `Result ${action === "approve" ? "approved" : "rejected"}`);
      await load();
      await onChanged?.();
    } catch (error) {
      toast.error(error.message || "Review failed");
    } finally {
      setSaving(false);
    }
  }

  const display = selected ? shownValue(selected, mode) : null;
  const sourceUrl = selected
    ? `/api/records/file/${record.id}?inline=1${selected.source_page ? `#page=${selected.source_page}` : ""}`
    : `/api/records/file/${record.id}?inline=1`;

  return (
    <>
      <button
        onClick={() => setDialog(true)}
        className="p-2 rounded-lg hover:bg-accent text-muted-foreground"
        title="Review extracted results"
        aria-label={`Review extraction for ${record.title || record.filename}`}
      >
        <FileSearch className="w-4 h-4" />
      </button>
      {open && (
        <div className="fixed inset-0 z-50 bg-black/80 p-4 flex items-center justify-center" onMouseDown={() => setDialog(false)}>
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="medical-record-review-title"
            aria-describedby="medical-record-review-description"
            className="relative grid w-full max-w-6xl h-[88vh] grid-rows-[auto_1fr] gap-4 overflow-hidden rounded-lg border bg-background p-6 shadow-lg"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="flex flex-col space-y-1.5 text-center sm:text-left">
              <h2 id="medical-record-review-title" className="text-lg font-semibold leading-none tracking-tight">Verify extracted medical data</h2>
              <p id="medical-record-review-description" className="text-sm text-muted-foreground">
              Compare each result with its source. Parser confidence describes extraction certainty, not clinical validity.
              </p>
            </div>
            <button onClick={() => setDialog(false)} className="absolute right-4 top-4 text-muted-foreground hover:text-foreground" aria-label="Close">×</button>
          {loading && !data ? (
            <div className="flex items-center justify-center text-sm text-muted-foreground"><Loader2 className="w-4 h-4 animate-spin mr-2" /> Loading extraction…</div>
          ) : !data?.observations?.length ? (
            <div className="text-sm text-muted-foreground py-8 text-center">
              No audited extraction is available yet. Reprocess this document to create one.
            </div>
          ) : (
            <div className="grid min-h-0 gap-4 lg:grid-cols-[minmax(0,1.25fr)_minmax(380px,0.75fr)]">
              <div className="min-h-0 rounded-lg border border-border overflow-hidden bg-muted/20 flex flex-col">
                <div className="px-3 py-2 border-b border-border flex items-center justify-between text-xs">
                  <span>Source {selected?.source_page ? `· page ${selected.source_page}` : "· document location unavailable"}</span>
                  <a href={sourceUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">
                    Open original <ExternalLink className="w-3 h-3" />
                  </a>
                </div>
                <iframe title="Medical record source preview" src={sourceUrl} className="w-full flex-1 bg-white" />
              </div>

              <div className="min-h-0 flex flex-col gap-3">
                <div className="max-h-44 overflow-auto rounded-lg border border-border divide-y divide-border">
                  {data.observations.map((item) => (
                    <button key={item.id} onClick={() => setSelectedId(item.id)} className={`w-full text-left px-3 py-2 text-xs hover:bg-accent ${selected?.id === item.id ? "bg-accent" : ""}`}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium truncate">{item.normalized_name}</span>
                        <span className={`rounded-full px-1.5 py-0.5 text-[9px] uppercase ${STATUS_TONE[item.verification_status] || "bg-muted text-muted-foreground"}`}>{item.verification_status}</span>
                      </div>
                      <div className="text-muted-foreground mt-0.5">{item.original_value} {item.normalized_unit} · page {item.source_page || "?"}</div>
                    </button>
                  ))}
                </div>

                {selected && (
                  <div className="min-h-0 overflow-auto rounded-lg border border-border p-3 space-y-3">
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex rounded-md border border-border overflow-hidden">
                        {["original", "normalized"].map((key) => (
                          <button key={key} onClick={() => setMode(key)} className={`px-2.5 py-1 text-xs capitalize ${mode === key ? "bg-primary text-primary-foreground" : "hover:bg-muted"}`}>{key}</button>
                        ))}
                      </div>
                      <span className="text-[10px] text-muted-foreground">Confidence {selected.parser_confidence == null ? "not reported" : `${Math.round(selected.parser_confidence * 100)}%`}</span>
                    </div>

                    {editing ? (
                      <div className="grid grid-cols-2 gap-2">
                        <label className="col-span-2 text-[10px] text-muted-foreground">Test name<input className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm" value={edit.test_name} onChange={(event) => setEdit({ ...edit, test_name: event.target.value })} /></label>
                        <label className="text-[10px] text-muted-foreground">Value<input className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm" value={edit.value} onChange={(event) => setEdit({ ...edit, value: event.target.value })} /></label>
                        <label className="text-[10px] text-muted-foreground">Kind<select value={edit.value_kind} onChange={(event) => setEdit({ ...edit, value_kind: event.target.value })} className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"><option value="numeric">Numeric</option><option value="qualitative">Qualitative</option><option value="titer">Titer</option></select></label>
                        <label className="text-[10px] text-muted-foreground">Unit<input className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm" value={edit.unit} onChange={(event) => setEdit({ ...edit, unit: event.target.value })} /></label>
                        <label className="text-[10px] text-muted-foreground">Specimen<input className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm" value={edit.specimen} onChange={(event) => setEdit({ ...edit, specimen: event.target.value })} /></label>
                        <label className="text-[10px] text-muted-foreground">Reference low<input className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm" type="number" value={edit.reference_low} onChange={(event) => setEdit({ ...edit, reference_low: event.target.value })} /></label>
                        <label className="text-[10px] text-muted-foreground">Reference high<input className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm" type="number" value={edit.reference_high} onChange={(event) => setEdit({ ...edit, reference_high: event.target.value })} /></label>
                        <label className="text-[10px] text-muted-foreground">Flag<input className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm" value={edit.flag} onChange={(event) => setEdit({ ...edit, flag: event.target.value })} /></label>
                        <label className="text-[10px] text-muted-foreground">Collected date<input className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm" type="date" value={edit.collected_date} onChange={(event) => setEdit({ ...edit, collected_date: event.target.value })} /></label>
                        <label className="col-span-2 text-[10px] text-muted-foreground">Category<input className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm" value={edit.category} onChange={(event) => setEdit({ ...edit, category: event.target.value })} /></label>
                      </div>
                    ) : (
                      <dl className="grid grid-cols-[100px_1fr] gap-x-3 gap-y-1 text-sm">
                        <dt className="text-muted-foreground">Name</dt><dd>{display.name || "—"}</dd>
                        <dt className="text-muted-foreground">Value</dt><dd className="font-medium">{display.value === "" || display.value == null ? "—" : display.value} {display.unit}</dd>
                        <dt className="text-muted-foreground">Range</dt><dd>{display.range || "—"}</dd>
                        <dt className="text-muted-foreground">Flag</dt><dd>{display.flag || "—"}</dd>
                        <dt className="text-muted-foreground">Specimen</dt><dd>{selected.specimen || "—"}</dd>
                        <dt className="text-muted-foreground">Collected</dt><dd>{display.date || "—"}</dd>
                        <dt className="text-muted-foreground">Location</dt><dd>{selected.extraction_location?.description || "Not reported"}</dd>
                      </dl>
                    )}

                    {selected.validation_issues?.length > 0 && (
                      <div className="rounded-md bg-amber-50 border border-amber-200 p-2 text-xs text-amber-900 space-y-1">
                        {selected.validation_issues.map((issue) => <div key={issue.code} className="flex gap-1.5"><AlertTriangle className="w-3 h-3 mt-0.5 flex-none" />{issue.message}</div>)}
                      </div>
                    )}

                    {isAdmin && selected.verification_status !== "rejected" && (
                      <div className="flex flex-wrap gap-2 pt-1">
                        {editing ? (
                          <>
                            <button onClick={() => review("edit")} disabled={saving} className="h-8 rounded-md bg-primary px-3 text-xs text-primary-foreground disabled:opacity-50">Save correction</button>
                            <button onClick={() => setEditing(false)} disabled={saving} className="h-8 rounded-md border border-input px-3 text-xs disabled:opacity-50">Cancel</button>
                          </>
                        ) : (
                          <>
                            {selected.verification_status !== "approved" && selected.verification_status !== "edited" && (
                              <button onClick={() => review("approve")} disabled={saving || selected.validation_status === "invalid"} className="h-8 rounded-md bg-primary px-3 text-xs text-primary-foreground disabled:opacity-50 inline-flex items-center gap-1"><Check className="w-3.5 h-3.5" /> Approve</button>
                            )}
                            <button onClick={() => setEditing(true)} disabled={saving} className="h-8 rounded-md border border-input px-3 text-xs disabled:opacity-50 inline-flex items-center gap-1"><Pencil className="w-3.5 h-3.5" /> Edit</button>
                            <button onClick={() => review("reject")} disabled={saving} className="h-8 rounded-md border border-input px-3 text-xs text-destructive disabled:opacity-50 inline-flex items-center gap-1"><XCircle className="w-3.5 h-3.5" /> Reject</button>
                          </>
                        )}
                      </div>
                    )}
                    {selected.history?.length > 0 && <p className="text-[10px] text-muted-foreground">{selected.history.length} recorded review event{selected.history.length === 1 ? "" : "s"}; correction history is append-only.</p>}
                  </div>
                )}
              </div>
            </div>
          )}
          </div>
        </div>
      )}
    </>
  );
}
