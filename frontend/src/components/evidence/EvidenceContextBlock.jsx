import ClaimEvidenceDialog from "@/components/evidence/ClaimEvidenceDialog";
import { AlertTriangle, Database, ExternalLink, FileSearch, ShieldCheck } from "lucide-react";

function humanize(value) {
  return String(value || "evidence")
    .replaceAll("_", " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2");
}

function sourceLabel(link) {
  if (link.kind === "claim_evidence") return `Open ${humanize(link.entity_type)} evidence`;
  if (link.kind === "source_document") return "Open source document";
  return `Open ${humanize(link.entity_type)} source`;
}

export default function EvidenceContextBlock({ context, narrativeEvidenceIds = [] }) {
  if (!context?.bundle) return null;

  const referenced = new Set(narrativeEvidenceIds || []);
  const referencedLinks = (context.evidence_items || [])
    .filter((item) => referenced.has(item.id))
    .flatMap((item) => item.source_links || []);
  const sourcePool = referencedLinks.length > 0 ? referencedLinks : (context.sources?.links || []);
  const sources = [...new Map(sourcePool.map((link) => [`${link.kind}:${link.href}`, link])).values()];
  const unverifiedLabs = (context.evidence_items || []).filter(
    (item) => item.entity_type === "LabResult" && item.confidence?.clinically_verified === false,
  ).length;
  const blocking = (context.contradictions || []).filter((item) => item.severity === "blocking").length;

  return (
    <div className="report-section report-card rounded-xl border border-border bg-card p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold text-sm flex items-center gap-2">
            <ShieldCheck className="w-4 h-4 text-primary" /> Shared evidence context
          </h2>
          <p className="text-[11px] text-muted-foreground mt-1">
            Deterministic Evidence Bundle {context.bundle.version} · narrative and report claims use the same governed sources.
          </p>
        </div>
        <span className="text-[9px] uppercase tracking-wide text-muted-foreground">{context.contract_version}</span>
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        <div className="rounded-lg bg-muted/50 p-3">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground flex items-center gap-1">
            <Database className="w-3 h-3" /> Data through
          </div>
          <ul className="mt-1 text-xs space-y-0.5">
            {(context.data_through || []).map((item) => (
              <li key={item.domain} className="flex justify-between gap-2">
                <span className="capitalize">{humanize(item.domain)}</span>
                <span className="text-muted-foreground tabular-nums">{item.through || "No dated evidence"}</span>
              </li>
            ))}
          </ul>
        </div>
        <div className="rounded-lg bg-muted/50 p-3">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Data quality</div>
          <ul className="mt-1 text-xs space-y-0.5">
            {(context.data_quality || []).map((item) => (
              <li key={item.domain} className="flex justify-between gap-2">
                <span className="capitalize">{humanize(item.domain)}</span>
                <span className="text-muted-foreground">
                  {humanize(item.coverage_status || "not assessed")} · {humanize(item.freshness_status || "unknown freshness")}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      {context.source_diagnostics?.length > 0 && (
        <div className="rounded-lg bg-muted/50 p-3">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground flex items-center gap-1">
            <Database className="w-3 h-3" /> Source data through
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">
            Operational source freshness, separate from health findings.
          </p>
          <ul className="mt-2 grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
            {context.source_diagnostics.map((source) => (
              <li key={source.source} className="flex justify-between gap-2">
                <span>{source.label}</span>
                <span className="text-muted-foreground tabular-nums">
                  {source.data_through ? source.data_through.slice(0, 10) : "No dated data"} · {humanize(source.status)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {(unverifiedLabs > 0 || (context.contradictions || []).length > 0) && (
        <div className="rounded-lg border border-amber-200 bg-amber-50/70 p-3 text-xs text-amber-900 space-y-1">
          {unverifiedLabs > 0 && (
            <p>{unverifiedLabs} machine-extracted lab result{unverifiedLabs === 1 ? " is" : "s are"} explicitly qualified as unverified.</p>
          )}
          {(context.contradictions || []).length > 0 && (
            <p className="flex gap-1.5"><AlertTriangle className="w-3.5 h-3.5 mt-0.5" />
              {context.contradictions.length} unresolved contradiction{context.contradictions.length === 1 ? "" : "s"} retain both sides{blocking ? `; ${blocking} blocking` : ""}.
            </p>
          )}
        </div>
      )}

      {context.claims?.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">Evidence-backed claims</div>
          <ul className="space-y-1">
            {context.claims.map((claim) => (
              <li key={`${claim.claim_type}:${claim.claim_id}`} className="flex items-center justify-between gap-2 rounded border border-border px-2 py-1.5 text-xs">
                <span className="min-w-0 truncate">{claim.title}</span>
                <span className="print:hidden"><ClaimEvidenceDialog claimType={claim.claim_type} claimId={claim.claim_id} /></span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {sources.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1 flex items-center gap-1">
            <FileSearch className="w-3 h-3" /> {referencedLinks.length ? "Narrative sources" : "Source evidence"}
          </div>
          <div className="flex flex-wrap gap-2">
            {sources.slice(0, 8).map((link) => (
              <a key={`${link.kind}:${link.href}`} href={link.href} target="_blank" rel="noreferrer" className="text-[11px] rounded-full border border-border px-2 py-1 hover:bg-muted">
                {sourceLabel(link)} <ExternalLink className="w-2.5 h-2.5 inline" />
              </a>
            ))}
            {sources.length > 8 && <span className="text-[11px] text-muted-foreground self-center">+{sources.length - 8} more governed sources</span>}
          </div>
        </div>
      )}
    </div>
  );
}
