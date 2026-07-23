import { useEffect, useMemo, useState } from "react";
import { Download, Eye, FileDown, Loader2, ShieldCheck } from "lucide-react";
import { useAuth } from "@/lib/AuthContext";

const MODE_OPTIONS = [
  {
    value: "full_private",
    label: "Full private copy",
    description: "A broad owner-only record using explicit health-data field allowlists.",
    adminOnly: true,
  },
  {
    value: "clinician",
    label: "Clinician copy",
    description: "A minimum-necessary, evidence-linked summary for clinical review.",
  },
  {
    value: "emergency",
    label: "Emergency summary",
    description: "A short conditions, medications, allergies, and health-profile summary.",
  },
  {
    value: "anonymized_research",
    label: "Anonymized research copy",
    description: "Bounded Evidence Bundle observations with relative dates and no direct identifiers.",
    adminOnly: true,
  },
  {
    value: "demo",
    label: "Synthetic demo copy",
    description: "Static synthetic data that never includes the account's health records.",
    adminOnly: true,
  },
];

const RANGE_OPTIONS = [30, 90, 180, 365];

async function readError(response, fallback) {
  try {
    const body = await response.json();
    return body.detail || fallback;
  } catch {
    return fallback;
  }
}

export default function ShareExports() {
  const { isAdmin } = useAuth();
  const options = useMemo(
    () => MODE_OPTIONS.filter((option) => !option.adminOnly || isAdmin),
    [isAdmin],
  );
  const [mode, setMode] = useState(() => (isAdmin ? "full_private" : "clinician"));
  const [days, setDays] = useState(90);
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!options.some((option) => option.value === mode)) {
      setMode(options[0]?.value || "clinician");
    }
  }, [mode, options]);

  const changeMode = (event) => {
    setMode(event.target.value);
    setPreview(null);
    setError("");
  };

  const changeDays = (event) => {
    setDays(Number(event.target.value));
    setPreview(null);
    setError("");
  };

  const generatePreview = async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetch("/api/share-exports/preview", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, days }),
      });
      if (!response.ok) {
        throw new Error(await readError(response, `Preview failed (${response.status})`));
      }
      setPreview(await response.json());
    } catch (failure) {
      setPreview(null);
      setError(failure.message);
    } finally {
      setLoading(false);
    }
  };

  const download = async () => {
    if (!preview) return;
    setDownloading(true);
    setError("");
    try {
      const response = await fetch("/api/share-exports/download", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(preview.request),
      });
      if (!response.ok) {
        throw new Error(await readError(response, `Download failed (${response.status})`));
      }
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `glucopilot-${preview.request.mode}-export.json`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setDownloading(false);
    }
  };

  const policy = preview?.export?.policy;

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <div>
        <h1 className="flex items-center gap-2 text-lg font-semibold">
          <ShieldCheck className="h-5 w-5 text-primary" />
          Share-safe exports
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Preview the exact privacy-reviewed payload before downloading it.
        </p>
      </div>

      <section className="space-y-4 rounded-xl border border-border bg-card p-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="space-y-1 text-sm">
            <span className="font-medium">Export mode</span>
            <select
              aria-label="Export mode"
              value={mode}
              onChange={changeMode}
              className="h-10 w-full rounded-md border bg-background px-3"
            >
              {options.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label className="space-y-1 text-sm">
            <span className="font-medium">Data window</span>
            <select
              aria-label="Export date range"
              value={days}
              onChange={changeDays}
              className="h-10 w-full rounded-md border bg-background px-3"
            >
              {RANGE_OPTIONS.map((value) => (
                <option key={value} value={value}>{value} days</option>
              ))}
            </select>
          </label>
        </div>

        <p className="text-sm text-muted-foreground">
          {options.find((option) => option.value === mode)?.description}
        </p>

        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
          Every mode uses an explicit allowlist. Insurance and prescription identifiers,
          employer, email, credentials, tokens, secret URLs, and internal IDs are excluded.
        </div>

        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={generatePreview}
            disabled={loading}
            className="flex h-10 items-center gap-2 rounded-md bg-primary px-4 text-sm text-primary-foreground disabled:opacity-50"
          >
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Eye className="h-4 w-4" />}
            Generate privacy preview
          </button>
          <button
            type="button"
            onClick={download}
            disabled={!preview || downloading}
            className="flex h-10 items-center gap-2 rounded-md border px-4 text-sm disabled:opacity-50"
          >
            {downloading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
            Download exact preview
          </button>
        </div>
      </section>

      {error && (
        <p role="alert" className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </p>
      )}

      {preview && (
        <section className="space-y-4 rounded-xl border border-border bg-card p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="flex items-center gap-2 font-semibold">
                <FileDown className="h-4 w-4 text-primary" />
                Exact export preview
              </h2>
              <p className="mt-1 text-xs text-muted-foreground">
                Generated {policy.generated_at} · Expires {policy.expires_at}
              </p>
            </div>
            <span className="rounded-md border border-primary/30 bg-primary/5 px-3 py-1 text-xs font-semibold">
              {policy.watermark}
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            Policy {policy.version} · Checksum {preview.export.checksum}
          </p>
          <pre
            aria-label="Exact export JSON preview"
            className="max-h-[32rem] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-muted p-4 text-xs"
          >
            {JSON.stringify(preview.export, null, 2)}
          </pre>
        </section>
      )}
    </div>
  );
}
