import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Database,
  GitBranch,
  HardDrive,
  Loader2,
  RefreshCw,
  ShieldCheck,
  XCircle,
} from "lucide-react";

const STATUS_STYLE = {
  healthy: "border-emerald-200 bg-emerald-50 text-emerald-800",
  current: "border-emerald-200 bg-emerald-50 text-emerald-800",
  warning: "border-amber-200 bg-amber-50 text-amber-900",
  stale: "border-amber-200 bg-amber-50 text-amber-900",
  critical: "border-red-200 bg-red-50 text-red-800",
  error: "border-red-200 bg-red-50 text-red-800",
  unavailable: "border-slate-200 bg-slate-50 text-slate-700",
  inactive: "border-slate-200 bg-slate-50 text-slate-600",
};

function humanize(value) {
  return String(value || "unknown").replaceAll("_", " ");
}

function when(value) {
  if (!value) return "Not available";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? "Not available" : parsed.toLocaleString();
}

function bytes(value) {
  const amount = Number(value || 0);
  if (amount < 1024) return `${amount} B`;
  if (amount < 1024 ** 2) return `${(amount / 1024).toFixed(1)} KB`;
  if (amount < 1024 ** 3) return `${(amount / 1024 ** 2).toFixed(1)} MB`;
  return `${(amount / 1024 ** 3).toFixed(1)} GB`;
}

function duration(value) {
  if (value === null || value === undefined) return "Not available";
  if (value < 60) return `${value} sec`;
  if (value < 3600) return `${Math.round(value / 60)} min`;
  return `${(value / 3600).toFixed(1)} hr`;
}

function StatusBadge({ status }) {
  const Icon = ["critical", "error"].includes(status)
    ? XCircle
    : ["warning", "stale"].includes(status)
      ? AlertTriangle
      : CheckCircle2;
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${STATUS_STYLE[status] || STATUS_STYLE.unavailable}`}>
      <Icon className="h-3 w-3" /> {humanize(status)}
    </span>
  );
}

function Metric({ label, value }) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-sm">{value}</dd>
    </div>
  );
}

export default function Diagnostics() {
  const [diagnostics, setDiagnostics] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(false);
  const activeRequest = useRef(null);

  const load = useCallback(async () => {
    if (activeRequest.current) return;
    const controller = new AbortController();
    activeRequest.current = controller;
    if (mounted.current) {
      setLoading(true);
      setError("");
    }
    try {
      const response = await fetch("/api/diagnostics", {
        credentials: "same-origin",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Diagnostics request failed (${response.status})`);
      const body = await response.json();
      if (mounted.current && !controller.signal.aborted) setDiagnostics(body);
    } catch (failure) {
      if (mounted.current && !controller.signal.aborted) {
        setError(failure.message || "Diagnostics are temporarily unavailable.");
      }
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null;
      if (mounted.current && !controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    load();
    return () => {
      mounted.current = false;
      activeRequest.current?.abort();
      activeRequest.current = null;
    };
  }, [load]);

  const qualityCounters = diagnostics?.quality?.counters || {};
  const backup = diagnostics?.storage?.backup;

  return (
    <div className="mx-auto max-w-6xl space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-semibold">
            <Activity className="h-5 w-5 text-primary" /> Platform diagnostics
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Data-source freshness, pipeline quality, analytics, graph, storage, and backup visibility.
          </p>
        </div>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          className="inline-flex h-9 items-center gap-1.5 rounded-md border border-border px-3 text-sm hover:bg-muted disabled:opacity-60"
        >
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          Refresh
        </button>
      </div>

      <div className="rounded-lg border border-sky-200 bg-sky-50 p-3 text-sm text-sky-900">
        <span className="font-medium">Operational diagnostics only.</span>{" "}
        These statuses describe data pipelines and platform state. They are not medical findings
        and do not assess your health.
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </div>
      )}

      {!diagnostics && loading && (
        <div className="flex items-center justify-center gap-2 rounded-xl border border-border bg-card p-10 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading operational diagnostics…
        </div>
      )}

      {diagnostics && (
        <>
          <section className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-card p-4">
            <div>
              <div className="text-xs uppercase tracking-wide text-muted-foreground">Overall platform status</div>
              <div className="mt-1"><StatusBadge status={diagnostics.status} /></div>
            </div>
            <div className="text-right text-xs text-muted-foreground">
              Generated {when(diagnostics.generated_at)}
              <div className="mt-0.5 font-mono text-[10px]">{diagnostics.contract_version}</div>
            </div>
          </section>

          <section className="space-y-3">
            <div>
              <h2 className="font-semibold">Data sources</h2>
              <p className="text-xs text-muted-foreground">
                A source failure is an ingestion problem, not a health finding.
              </p>
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              {diagnostics.sources.map((source) => (
                <article key={source.source} className="rounded-xl border border-border bg-card p-4">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <h3 className="font-medium">{source.label}</h3>
                      <p className="text-[11px] text-muted-foreground">
                        {source.configured ? "Configured" : "Not configured"} · {humanize(source.tracking)} tracking
                      </p>
                    </div>
                    <StatusBadge status={source.status} />
                  </div>
                  <dl className="mt-3 grid grid-cols-2 gap-3">
                    <Metric label="Data through" value={when(source.data_through)} />
                    <Metric label="Last successful sync" value={when(source.last_successful_sync_at)} />
                    <Metric label="Import lag" value={duration(source.import_lag_seconds)} />
                    <Metric
                      label="Freshness"
                      value={source.freshness_days === null ? "Not available" : `${source.freshness_days} days`}
                    />
                  </dl>
                  {source.issues.length > 0 && (
                    <ul className="mt-3 space-y-1 border-t border-border pt-3 text-xs text-muted-foreground">
                      {source.issues.map((issue) => <li key={issue.code}>{issue.message}</li>)}
                    </ul>
                  )}
                </article>
              ))}
            </div>
          </section>

          <section className="grid gap-3 lg:grid-cols-3">
            <article className="rounded-xl border border-border bg-card p-4">
              <h2 className="flex items-center justify-between gap-2 font-semibold">
                <span className="flex items-center gap-2"><ShieldCheck className="h-4 w-4 text-primary" /> Data quality</span>
                <StatusBadge status={diagnostics.quality.status} />
              </h2>
              <dl className="mt-3 grid grid-cols-2 gap-3">
                <Metric label="Failed sync runs" value={qualityCounters.sync_failed_runs || 0} />
                <Metric label="Partial sync runs" value={qualityCounters.sync_partial_runs || 0} />
                <Metric label="Failed sync items" value={qualityCounters.sync_failed_items || 0} />
                <Metric label="Duplicates/skips" value={qualityCounters.sync_duplicate_or_skipped_items || 0} />
                <Metric label="Parser failures" value={(qualityCounters.parser_failed_runs || 0) + (qualityCounters.parser_failed_batches || 0)} />
                <Metric label="Unverified records" value={qualityCounters.unverified_records || 0} />
                <Metric label="Invalid records" value={qualityCounters.invalid_records || 0} />
                <Metric label="Unresolved times" value={qualityCounters.unresolved_canonical_times || 0} />
              </dl>
            </article>

            <article className="rounded-xl border border-border bg-card p-4">
              <h2 className="flex items-center justify-between gap-2 font-semibold">
                <span className="flex items-center gap-2"><GitBranch className="h-4 w-4 text-primary" /> Derived state</span>
              </h2>
              <div className="mt-3 space-y-4">
                <div>
                  <div className="flex items-center justify-between gap-2 text-sm">
                    <span>Relationship graph</span><StatusBadge status={diagnostics.graph.status} />
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">Published {when(diagnostics.graph.published_at)}</p>
                </div>
                <div>
                  <div className="flex items-center justify-between gap-2 text-sm">
                    <span>Analytics</span><StatusBadge status={diagnostics.analytics.status} />
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">Generated {when(diagnostics.analytics.latest_generated_at)}</p>
                </div>
              </div>
            </article>

            <article className="rounded-xl border border-border bg-card p-4">
              <h2 className="flex items-center gap-2 font-semibold">
                <HardDrive className="h-4 w-4 text-primary" /> Storage and backups
              </h2>
              <dl className="mt-3 grid grid-cols-2 gap-3">
                <Metric label="Database size" value={bytes(diagnostics.storage.database_bytes)} />
                <Metric label="WAL size" value={bytes(diagnostics.storage.wal_bytes)} />
                <Metric label="Latest backup" value={when(backup.latest_created_at)} />
                <Metric label="Backup age" value={backup.age_days === null ? "Not available" : `${backup.age_days} days`} />
              </dl>
              <div className="mt-3 flex items-center justify-between gap-2 border-t border-border pt-3 text-xs">
                <span className="flex items-center gap-1 text-muted-foreground"><Database className="h-3 w-3" /> Checksummed manifest visibility</span>
                <StatusBadge status={backup.status} />
              </div>
            </article>
          </section>
        </>
      )}
    </div>
  );
}
