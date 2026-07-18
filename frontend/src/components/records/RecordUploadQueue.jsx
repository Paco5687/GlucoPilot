import { useState, useRef, useEffect, useCallback, useMemo } from "react";
import { Button } from "@/components/ui/button";
import { Upload, Loader2, CheckCircle2, XCircle, Copy, FileText, X, Trash2 } from "lucide-react";

const ACCEPT = /\.(pdf|png|jpe?g|webp)$/i;

const STATUS = {
  queued: { icon: FileText, cls: "text-muted-foreground", label: "Queued" },
  processing: { icon: Loader2, cls: "text-primary animate-spin", label: "Extracting…" },
  done: { icon: CheckCircle2, cls: "text-green-500", label: "Done" },
  duplicate: { icon: Copy, cls: "text-amber-500", label: "Duplicate" },
  failed: { icon: XCircle, cls: "text-destructive", label: "Failed" },
};

let _uid = 0;

export default function RecordUploadQueue({ onComplete }) {
  const [items, setItems] = useState([]);
  const [running, setRunning] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef(null);
  const drainedRef = useRef(true);

  const addFiles = useCallback((fileList) => {
    const incoming = Array.from(fileList || []);
    setItems((prev) => {
      const seen = new Set(prev.map((i) => i.file.name + ":" + i.file.size));
      const add = [];
      for (const f of incoming) {
        if (!ACCEPT.test(f.name)) continue;
        const key = f.name + ":" + f.size;
        if (seen.has(key)) continue; // de-dupe within the picker selection
        seen.add(key);
        add.push({ id: ++_uid, file: f, status: "queued", labs: null, error: null, dupOf: null });
      }
      return add.length ? [...prev, ...add] : prev;
    });
  }, []);

  // Sequential processor: one file at a time (keeps the local vision model sane).
  useEffect(() => {
    if (running) return;
    const next = items.find((i) => i.status === "queued");
    if (!next) return;
    drainedRef.current = false;
    setRunning(true);
    (async () => {
      setItems((p) => p.map((i) => (i.id === next.id ? { ...i, status: "processing" } : i)));
      try {
        const form = new FormData();
        form.append("file", next.file);
        const res = await fetch("/api/records/upload", { method: "POST", body: form, credentials: "same-origin" });
        const data = await res.json().catch(() => null);
        if (!res.ok) throw new Error(data?.detail || `Failed (${res.status})`);
        setItems((p) =>
          p.map((i) =>
            i.id === next.id
              ? { ...i, status: data.duplicate ? "duplicate" : "done", labs: data.lab_results ?? 0, dupOf: data.duplicate_of || null }
              : i
          )
        );
      } catch (err) {
        setItems((p) => p.map((i) => (i.id === next.id ? { ...i, status: "failed", error: err.message } : i)));
      } finally {
        setRunning(false);
      }
    })();
  }, [items, running]);

  // Fire onComplete once when the queue fully drains.
  useEffect(() => {
    const active = items.some((i) => i.status === "queued" || i.status === "processing");
    if (!active && items.length && !drainedRef.current) {
      drainedRef.current = true;
      onComplete?.();
    }
  }, [items, running, onComplete]);

  const counts = useMemo(() => {
    const c = { total: items.length, queued: 0, done: 0, duplicate: 0, failed: 0, processing: 0 };
    for (const i of items) c[i.status] = (c[i.status] || 0) + 1;
    return c;
  }, [items]);

  const finished = counts.done + counts.duplicate + counts.failed;
  const pct = counts.total ? Math.round((finished / counts.total) * 100) : 0;

  function onDrop(e) {
    e.preventDefault();
    setDragOver(false);
    addFiles(e.dataTransfer.files);
  }

  function retryFailed() {
    setItems((p) => p.map((i) => (i.status === "failed" ? { ...i, status: "queued", error: null } : i)));
  }

  return (
    <div className="space-y-3">
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        onClick={() => inputRef.current?.click()}
        className={`bg-card rounded-xl border border-dashed p-6 text-center cursor-pointer transition-colors ${dragOver ? "border-primary bg-primary/5" : "border-border"}`}
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.png,.jpg,.jpeg,.webp"
          className="hidden"
          onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }}
        />
        <Upload className="w-8 h-8 mx-auto mb-2 text-muted-foreground" />
        <p className="text-sm font-medium">Drop files here or click to choose</p>
        <p className="text-xs text-muted-foreground mt-1">
          PDF, PNG, or JPG — select many at once. Identical files are skipped automatically; each is read by the AI model one at a time.
        </p>
      </div>

      {items.length > 0 && (
        <div className="bg-card rounded-xl border border-border overflow-hidden">
          <div className="flex items-center justify-between gap-3 p-3 border-b border-border">
            <div className="flex items-center gap-2 text-sm">
              {running ? <Loader2 className="w-4 h-4 animate-spin text-primary" /> : <CheckCircle2 className="w-4 h-4 text-green-500" />}
              <span className="font-medium">{finished}/{counts.total} processed</span>
              <span className="text-xs text-muted-foreground">
                {counts.done} new · {counts.duplicate} dupes · {counts.failed} failed
              </span>
            </div>
            <div className="flex items-center gap-2">
              {counts.failed > 0 && !running && (
                <Button variant="outline" size="sm" onClick={retryFailed} className="text-xs">Retry failed</Button>
              )}
              {!running && (
                <button
                  onClick={() => setItems((p) => p.filter((i) => i.status === "queued" || i.status === "processing"))}
                  className="text-xs text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
                  title="Clear finished"
                >
                  <Trash2 className="w-3.5 h-3.5" /> Clear finished
                </button>
              )}
            </div>
          </div>

          <div className="h-1 bg-muted">
            <div className="h-full bg-primary transition-all" style={{ width: `${pct}%` }} />
          </div>

          <div className="max-h-72 overflow-y-auto divide-y divide-border">
            {items.map((i) => {
              const s = STATUS[i.status];
              const Icon = s.icon;
              return (
                <div key={i.id} className="flex items-center gap-3 px-3 py-2 text-sm">
                  <Icon className={`w-4 h-4 flex-shrink-0 ${s.cls}`} />
                  <span className="flex-1 min-w-0 truncate">{i.file.name}</span>
                  <span className="text-xs text-muted-foreground flex-shrink-0">
                    {i.status === "done" && `${i.labs} lab${i.labs === 1 ? "" : "s"}`}
                    {i.status === "duplicate" && (i.dupOf ? `dup of ${i.dupOf}` : "duplicate")}
                    {i.status === "failed" && <span className="text-destructive" title={i.error}>{(i.error || "failed").slice(0, 40)}</span>}
                    {i.status === "queued" && "queued"}
                    {i.status === "processing" && s.label}
                  </span>
                  {i.status === "queued" && (
                    <button onClick={() => setItems((p) => p.filter((x) => x.id !== i.id))} className="text-muted-foreground hover:text-destructive flex-shrink-0" title="Remove">
                      <X className="w-3.5 h-3.5" />
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
