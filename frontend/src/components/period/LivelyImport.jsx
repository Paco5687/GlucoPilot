import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Upload, FileText, CheckCircle2, AlertCircle, Loader2 } from "lucide-react";
import { base44 } from "@/api/base44Client";

function parseCSV(text) {
  const lines = text.trim().split("\n");
  if (lines.length < 2) return [];
  const headers = lines[0].split(",").map((h) => h.trim().replace(/^"|"$/g, ""));
  return lines.slice(1).map((line) => {
    const values = line.split(",").map((v) => v.trim().replace(/^"|"$/g, ""));
    const obj = {};
    headers.forEach((h, i) => { obj[h] = values[i] || ""; });
    return obj;
  });
}

// Try to map common Lively export columns to our PeriodLog entity
function mapLivelyRow(row) {
  // Lively export format varies — we'll try common column names
  const date = row["Date"] || row["date"] || row["Day"] || row["day"] || "";
  if (!date) return null;

  // Normalize date to YYYY-MM-DD
  let normalizedDate = date;
  if (date.includes("/")) {
    const parts = date.split("/");
    if (parts.length === 3) {
      const [m, d, y] = parts;
      normalizedDate = `${y.length === 2 ? "20" + y : y}-${m.padStart(2, "0")}-${d.padStart(2, "0")}`;
    }
  }

  const phase = (row["Phase"] || row["phase"] || row["Cycle Phase"] || "").toLowerCase();
  const flow = (row["Flow"] || row["flow"] || row["Period Flow"] || row["Flow Intensity"] || "").toLowerCase();
  const symptoms = row["Symptoms"] || row["symptoms"] || "";
  const notes = row["Notes"] || row["notes"] || "";

  const phaseMap = {
    menstrual: "menstrual", period: "menstrual", bleeding: "menstrual",
    follicular: "follicular",
    ovulation: "ovulation", ovulatory: "ovulation", fertile: "ovulation",
    luteal: "luteal",
  };

  const flowMap = {
    none: "none", "no flow": "none",
    spotting: "spotting", spot: "spotting",
    light: "light",
    medium: "medium", moderate: "medium", normal: "medium",
    heavy: "heavy",
  };

  return {
    date: normalizedDate,
    phase: phaseMap[phase] || undefined,
    flow: flowMap[flow] || undefined,
    symptoms: symptoms || undefined,
    notes: notes || undefined,
    source: "lively_import",
  };
}

export default function LivelyImport({ onImportComplete }) {
  const [status, setStatus] = useState("idle"); // idle, parsing, importing, done, error
  const [preview, setPreview] = useState([]);
  const [rawHeaders, setRawHeaders] = useState([]);
  const [result, setResult] = useState(null);
  const [errorMsg, setErrorMsg] = useState("");

  const handleFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setStatus("parsing");
    const text = await file.text();
    const rows = parseCSV(text);

    if (!rows.length) {
      setStatus("error");
      setErrorMsg("No data rows found in CSV.");
      return;
    }

    setRawHeaders(Object.keys(rows[0]));
    const mapped = rows.map(mapLivelyRow).filter(Boolean).filter((r) => r.date);
    setPreview(mapped);
    setStatus(mapped.length ? "preview" : "error");
    if (!mapped.length) setErrorMsg("Could not find date column in CSV. Expected columns: Date, Phase, Flow, Symptoms, Notes.");
  };

  const handleImport = async () => {
    setStatus("importing");
    let imported = 0;
    let skipped = 0;

    // Check existing dates to avoid duplicates
    const existing = await base44.entities.PeriodLog.list("-date", 5000);
    const existingDates = new Set(existing.map((l) => l.date));

    const toCreate = preview.filter((r) => !existingDates.has(r.date));
    skipped = preview.length - toCreate.length;

    // Bulk create in batches of 50
    for (let i = 0; i < toCreate.length; i += 50) {
      await base44.entities.PeriodLog.bulkCreate(toCreate.slice(i, i + 50));
      imported += Math.min(50, toCreate.length - i);
    }

    setResult({ imported, skipped });
    setStatus("done");
    onImportComplete?.();
  };

  return (
    <div className="bg-card rounded-xl border border-border p-4 space-y-4">
      <div className="flex items-center gap-2">
        <FileText className="w-4 h-4 text-primary" />
        <h3 className="text-sm font-semibold">Import from Lively</h3>
      </div>
      <p className="text-xs text-muted-foreground">
        Export your data from Lively (Settings → Export Data), then upload the CSV here.
      </p>

      {status === "idle" && (
        <div>
          <label className="cursor-pointer">
            <div className="border-2 border-dashed border-border rounded-lg p-6 text-center hover:border-primary/50 transition-colors">
              <Upload className="w-6 h-6 mx-auto mb-2 text-muted-foreground" />
              <span className="text-sm text-muted-foreground">Click to upload CSV</span>
            </div>
            <input type="file" accept=".csv" className="hidden" onChange={handleFile} />
          </label>
        </div>
      )}

      {status === "parsing" && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="w-4 h-4 animate-spin" /> Parsing file…
        </div>
      )}

      {status === "preview" && (
        <div className="space-y-3">
          <div className="text-sm">
            <span className="font-medium">{preview.length}</span> records found
          </div>
          <div className="text-xs text-muted-foreground">
            Detected columns: {rawHeaders.join(", ")}
          </div>
          <div className="max-h-40 overflow-y-auto text-xs font-mono bg-muted/50 rounded-lg p-2 space-y-1">
            {preview.slice(0, 10).map((r, i) => (
              <div key={i}>{r.date} — {r.phase || "?"} — {r.flow || "?"}</div>
            ))}
            {preview.length > 10 && <div className="text-muted-foreground">… and {preview.length - 10} more</div>}
          </div>
          <div className="flex gap-2">
            <Button onClick={handleImport} className="flex-1">Import {preview.length} Records</Button>
            <Button variant="outline" onClick={() => { setStatus("idle"); setPreview([]); }}>Cancel</Button>
          </div>
        </div>
      )}

      {status === "importing" && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="w-4 h-4 animate-spin" /> Importing…
        </div>
      )}

      {status === "done" && result && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-sm text-green-600">
            <CheckCircle2 className="w-4 h-4" />
            Imported {result.imported} records{result.skipped > 0 ? `, skipped ${result.skipped} duplicates` : ""}.
          </div>
          <Button variant="outline" size="sm" onClick={() => { setStatus("idle"); setPreview([]); setResult(null); }}>
            Import Another
          </Button>
        </div>
      )}

      {status === "error" && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-sm text-destructive">
            <AlertCircle className="w-4 h-4" /> {errorMsg}
          </div>
          <Button variant="outline" size="sm" onClick={() => { setStatus("idle"); setErrorMsg(""); }}>
            Try Again
          </Button>
        </div>
      )}
    </div>
  );
}