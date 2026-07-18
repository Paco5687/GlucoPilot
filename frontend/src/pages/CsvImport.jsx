import { useState, useRef } from "react";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Upload, CheckCircle, AlertCircle, Loader2, FileText } from "lucide-react";

const DATASET_CONFIGS = {
  cgm: {
    label: "CGM Readings",
    description: "Continuous glucose monitor data",
    timestampCol: "Timestamp (YYYY-MM-DDThh:mm:ss)",
    valueCol: "CGM Glucose Value (mmol/L)",
    process: (row) => {
      const ts = row["Timestamp (YYYY-MM-DDThh:mm:ss)"] || row["Timestamp"];
      const rawVal =
        row["CGM Glucose Value (mg/dL)"] ||
        row["CGM Glucose Value (mg/dl)"] ||
        row["CGM Glucose Value (mmol/L)"] ||
        row["CGM Glucose Value (mmol/l)"];
      if (!ts || !rawVal || isNaN(Number(rawVal))) return null;
      const isMgdl =
        row["CGM Glucose Value (mg/dL)"] !== undefined ||
        row["CGM Glucose Value (mg/dl)"] !== undefined;
      const val = isMgdl
        ? Math.round(Number(rawVal))
        : Math.round(Number(rawVal) * 18.015);
      return { value: val, timestamp: new Date(ts).toISOString(), source: "csv" };
    },
    entity: "GlucoseReading",
  },
  bg: {
    label: "BG Readings",
    description: "Manual blood glucose checks",
    process: (row) => {
      const ts = row["Timestamp (YYYY-MM-DDThh:mm:ss)"] || row["Timestamp"];
      const rawVal = row["BG Reading (mmol/L)"] || row["BG Reading (mg/dL)"];
      if (!ts || !rawVal || isNaN(Number(rawVal))) return null;
      const val = row["BG Reading (mg/dL)"]
        ? Math.round(Number(rawVal))
        : Math.round(Number(rawVal) * 18.015);
      return {
        type: "bg",
        event_type: "BG Check",
        timestamp: new Date(ts).toISOString(),
        glucose: val,
        source: "csv",
      };
    },
    entity: "Treatment",
  },
  bolus: {
    label: "Bolus",
    description: "Insulin bolus events",
    process: (row) => {
      const ts = row["Timestamp (YYYY-MM-DDThh:mm:ss)"] || row["Timestamp"];
      const amount = Number(row["Insulin Delivered (U)"] || row["Bolus Volume Delivered (U)"] || 0);
      if (!ts || !amount) return null;
      return {
        type: "insulin",
        event_type: row["Bolus Type"] || "Bolus",
        timestamp: new Date(ts).toISOString(),
        amount,
        insulin_type: "rapid",
        source: "csv",
      };
    },
    entity: "Treatment",
  },
  basal: {
    label: "Basal",
    description: "Basal rate events",
    process: (row) => {
      const ts = row["Timestamp (YYYY-MM-DDThh:mm:ss)"] || row["Timestamp"];
      const rate = Number(row["Basal Rate (U/hr)"] || row["Rate (U/hr)"] || 0);
      if (!ts) return null;
      return {
        type: "tempbasal",
        event_type: "Temp Basal",
        timestamp: new Date(ts).toISOString(),
        absolute: rate || undefined,
        duration: Number(row["Duration (minutes)"] || 0) || undefined,
        source: "csv",
      };
    },
    entity: "Treatment",
  },
  carbs: {
    label: "Carbs",
    description: "Carbohydrate entries",
    process: (row) => {
      const ts = row["Timestamp (YYYY-MM-DDThh:mm:ss)"] || row["Timestamp"];
      const amount = Number(row["Carbs (grams)"] || row["Carbohydrates (g)"] || 0);
      if (!ts || !amount) return null;
      return {
        type: "carb",
        event_type: "Carb Entry",
        timestamp: new Date(ts).toISOString(),
        amount,
        source: "csv",
      };
    },
    entity: "Treatment",
  },
  insulin: {
    label: "Insulin (Daily Totals)",
    description: "Daily insulin delivery totals",
    process: (row) => {
      const ts = row["Timestamp"];
      const bolus = Number(row["Total Bolus (U)"] || 0);
      const basal = Number(row["Total Basal (U)"] || 0);
      if (!ts) return null;
      if (!bolus && !basal) return null;
      return {
        type: "insulin",
        event_type: "Daily Total",
        timestamp: new Date(ts).toISOString(),
        amount: bolus || undefined,
        notes: `Bolus: ${bolus}U | Basal: ${basal}U | Total: ${Number(row["Total Insulin (U)"] || 0)}U`,
        insulin_type: "rapid",
        source: "csv",
      };
    },
    entity: "Treatment",
  },
  alarms: {
    label: "Alarms",
    description: "Pump/CGM alarm events",
    process: (row) => {
      const ts = row["Timestamp (YYYY-MM-DDThh:mm:ss)"] || row["Timestamp"];
      if (!ts) return null;
      return {
        type: "note",
        event_type: row["Alarm Type"] || row["Event Type"] || "Alarm",
        timestamp: new Date(ts).toISOString(),
        notes: row["Alarm Type"] || row["Description"] || "",
        source: "csv",
      };
    },
    entity: "Treatment",
  },
};

function parseCSV(text) {
  const lines = text.trim().split("\n");
  if (lines.length < 3) return [];
  // Row 1 is ignored (metadata), row 2 is headers, row 3+ is data
  const headers = lines[1].split(",").map((h) => h.trim().replace(/^"|"$/g, ""));
  return lines.slice(2).map((line) => {
    const values = line.split(",").map((v) => v.trim().replace(/^"|"$/g, ""));
    const row = {};
    headers.forEach((h, i) => (row[h] = values[i] || ""));
    return row;
  });
}

function DatasetImporter({ config, configKey }) {
  const [status, setStatus] = useState("idle"); // idle | loading | done | error
  const [result, setResult] = useState(null);
  const [preview, setPreview] = useState(null);
  const inputRef = useRef();

  const handleFile = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    setStatus("loading");
    setResult(null);

    const text = await file.text();
    const rows = parseCSV(text);
    if (rows.length === 0) {
      setStatus("error");
      setResult("CSV appears empty or could not be parsed.");
      return;
    }

    const detectedHeaders = Object.keys(rows[0]);
    const records = rows.map(config.process).filter(Boolean);

    setPreview({ total: rows.length, valid: records.length, headers: detectedHeaders });

    if (records.length === 0) {
      setStatus("error");
      setResult(`No valid records found. Detected columns: ${detectedHeaders.slice(0, 5).join(" | ")}`);
      return;
    }

    const entity = base44.entities[config.entity];
    let inserted = 0;
    for (let i = 0; i < records.length; i += 1000) {
      await entity.bulkCreate(records.slice(i, i + 1000));
      inserted += Math.min(1000, records.length - i);
      setResult(`Importing... ${inserted} / ${records.length}`);
      if (i + 1000 < records.length) {
        await new Promise((r) => setTimeout(r, 3000));
      }
    }

    setStatus("done");
    setResult(`Imported ${inserted} records`);
    e.target.value = "";
  };

  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="font-semibold text-sm">{config.label}</h3>
          <p className="text-xs text-muted-foreground mt-0.5">{config.description}</p>
        </div>
        {status === "done" && <CheckCircle className="w-5 h-5 text-green-500 shrink-0" />}
        {status === "error" && <AlertCircle className="w-5 h-5 text-red-500 shrink-0" />}
        {status === "loading" && <Loader2 className="w-5 h-5 text-primary animate-spin shrink-0" />}
      </div>

      {preview && (
        <p className="text-xs text-muted-foreground mb-2">
          {preview.valid} / {preview.total} rows valid
        </p>
      )}
      {result && (
        <p className={`text-xs mb-2 ${status === "error" ? "text-red-500" : "text-green-600"}`}>
          {result}
        </p>
      )}

      <input ref={inputRef} type="file" accept=".csv" className="hidden" onChange={handleFile} />
      <Button
        size="sm"
        variant="outline"
        className="w-full gap-2"
        disabled={status === "loading"}
        onClick={() => inputRef.current.click()}
      >
        <Upload className="w-4 h-4" />
        {status === "done" ? "Upload Again" : "Choose CSV"}
      </Button>
    </div>
  );
}

export default function CsvImport() {
  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Import CSV Data</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          Upload Glooko CSV exports. Each dataset type has its own uploader below.
        </p>
      </div>

      <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 flex gap-3 text-sm text-amber-800">
        <FileText className="w-4 h-4 shrink-0 mt-0.5" />
        <div>
          Export your data from <strong>Glooko → Reports → Export Data</strong> and select each dataset separately. 
          Upload each CSV file to the matching section below.
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {Object.entries(DATASET_CONFIGS).map(([key, config]) => (
          <DatasetImporter key={key} configKey={key} config={config} />
        ))}
      </div>
    </div>
  );
}