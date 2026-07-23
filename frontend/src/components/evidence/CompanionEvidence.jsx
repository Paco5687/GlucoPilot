import { useState } from "react";
import { base44 } from "@/api/base44Client";
import { ExternalLink, FileDiff, FileSearch, Loader2, Scale, ShieldAlert, X } from "lucide-react";

const CLASS_LABELS = {
  observation: "Observation",
  calculation: "Calculation",
  correlation: "Correlation",
  hypothesis: "Hypothesis",
  general_information: "General information",
  user_memory: "User memory",
  safety_guidance: "Safety guidance",
};

function sideText(side) {
  if (!side || typeof side !== "object") return "Unavailable";
  const value = side.value == null ? "" : String(side.value);
  return [side.label, value, side.unit].filter(Boolean).join(" · ") || "Recorded side";
}

function Sources({ items = [], external = [] }) {
  const links = items.flatMap((item) => item.source_links || []);
  if (!links.length && !external.length) {
    return <p className="text-xs text-muted-foreground">No openable source was attached.</p>;
  }
  return (
    <div className="flex flex-wrap gap-2">
      {links.map((link, index) => (
        <a
          key={`${link.kind}:${link.href}:${index}`}
          href={link.href}
          target="_blank"
          rel="noreferrer"
          className="text-[11px] rounded-full border border-border px-2 py-1 hover:bg-muted"
        >
          Open {link.entity_type || link.kind?.replaceAll("_", " ") || "evidence"}{" "}
          <ExternalLink className="w-2.5 h-2.5 inline" />
        </a>
      ))}
      {external.map((source) => (
        <a
          key={source.source_id}
          href={source.url}
          target="_blank"
          rel="noreferrer"
          className="text-[11px] rounded-full border border-border px-2 py-1 hover:bg-muted"
        >
          {source.source || "General source"} <ExternalLink className="w-2.5 h-2.5 inline" />
        </a>
      ))}
    </div>
  );
}

function ShowEvidence({ result }) {
  return (
    <div className="space-y-3">
      <div className="text-[11px] text-muted-foreground">
        Evidence Bundle {result.bundle?.version || "2.0.0"} · {result.evidence_items?.length || 0} cited items
      </div>
      <ul className="space-y-2">
        {(result.statements || []).map((statement) => (
          <li key={statement.ordinal} className="rounded-lg border border-border p-2.5 text-xs">
            <div className="flex flex-wrap gap-1.5 mb-1">
              <span className="rounded-full bg-primary/10 text-primary px-2 py-0.5">
                {CLASS_LABELS[statement.classification] || statement.classification}
              </span>
              {(statement.evidence_aliases || []).map((alias) => (
                <span key={alias} className="rounded-full bg-muted px-2 py-0.5">{alias}</span>
              ))}
            </div>
            <p>{statement.text}</p>
          </li>
        ))}
      </ul>
      <Sources items={result.evidence_items} external={result.external_sources} />
      {!!result.missing_data_caveats?.length && (
        <p className="text-[11px] text-amber-700">
          {result.missing_data_caveats.length} data limitation
          {result.missing_data_caveats.length === 1 ? "" : "s"} recorded.
        </p>
      )}
    </div>
  );
}

function OpposingEvidence({ result }) {
  const items = result.opposing_evidence || [];
  const contradictions = result.contradictions || [];
  if (!items.length && !contradictions.length) {
    return <p className="text-xs text-muted-foreground">No explicit opposing evidence or unresolved contradiction was selected.</p>;
  }
  return (
    <div className="space-y-2">
      {items.map((item, index) => (
        <div key={`${item.evidence_item_id || "opposing"}:${index}`} className="rounded-lg border border-border p-2.5 text-xs">
          <span className="font-medium">{item.evidence_alias || "Opposing evidence"}</span>
          {item.reason && <span className="ml-1 text-muted-foreground">— {item.reason}</span>}
        </div>
      ))}
      {contradictions.map((item) => (
        <div key={item.id} className="rounded-lg border border-amber-200 bg-amber-50/60 p-2.5 text-xs text-amber-950">
          <p className="font-medium">{item.explanation || "Unresolved contradiction"}</p>
          <div className="grid sm:grid-cols-2 gap-2 mt-2">
            <div className="rounded bg-white/60 p-2">{sideText(item.left)}</div>
            <div className="rounded bg-white/60 p-2">{sideText(item.right)}</div>
          </div>
          <p className="mt-1 text-[11px]">Neither side was silently selected.</p>
        </div>
      ))}
    </div>
  );
}

function Changes({ result }) {
  return (
    <div className="text-xs space-y-2">
      <p className={result.changed ? "text-amber-700" : "text-emerald-700"}>
        {result.changed
          ? "The underlying evidence changed since this answer."
          : "The underlying evidence is unchanged since this answer."}
      </p>
      {!!result.changed_scopes?.length && (
        <p className="text-muted-foreground">
          Changed scopes: {result.changed_scopes.join(", ")}
        </p>
      )}
      <p className="text-[11px] text-muted-foreground">Checked {result.checked_at}</p>
    </div>
  );
}

export default function CompanionEvidence({ messageId, evidence }) {
  const [state, setState] = useState({ command: null, loading: false, result: null, error: null });
  if (!messageId || !evidence?.contract_version) return null;

  async function run(command) {
    if (state.command === command && state.result) {
      setState({ command: null, loading: false, result: null, error: null });
      return;
    }
    setState({ command, loading: true, result: null, error: null });
    try {
      const response = await base44.functions.invoke("companion", {
        action: "evidence_command",
        command,
        message_id: messageId,
      });
      setState({ command, loading: false, result: response.data, error: null });
    } catch (error) {
      setState({ command, loading: false, result: null, error });
    }
  }

  return (
    <div className="mt-1.5 max-w-[85%]">
      <div className="flex flex-wrap gap-1">
        <button type="button" onClick={() => run("show")} className="h-7 px-2 gap-1 text-[11px] inline-flex items-center rounded-md hover:bg-accent">
          <FileSearch className="w-3 h-3" /> Show evidence
        </button>
        <button type="button" onClick={() => run("opposing")} className="h-7 px-2 gap-1 text-[11px] inline-flex items-center rounded-md hover:bg-accent">
          <Scale className="w-3 h-3" /> What argues against this?
        </button>
        <button type="button" onClick={() => run("changes")} className="h-7 px-2 gap-1 text-[11px] inline-flex items-center rounded-md hover:bg-accent">
          <FileDiff className="w-3 h-3" /> What changed?
        </button>
      </div>
      {state.command && (
        <div className="relative mt-1 rounded-xl border border-border bg-background p-3 shadow-sm">
          <button
            type="button"
            onClick={() => setState({ command: null, loading: false, result: null, error: null })}
            className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
            aria-label="Close evidence"
          >
            <X className="w-3.5 h-3.5" />
          </button>
          {state.loading && <Loader2 className="w-4 h-4 animate-spin text-primary" aria-label="Loading evidence" />}
          {state.error && (
            <p className="text-xs text-destructive inline-flex gap-1">
              <ShieldAlert className="w-3.5 h-3.5" /> Evidence could not be loaded.
            </p>
          )}
          {state.result?.command === "show" && <ShowEvidence result={state.result} />}
          {state.result?.command === "opposing" && <OpposingEvidence result={state.result} />}
          {state.result?.command === "changes" && <Changes result={state.result} />}
        </div>
      )}
    </div>
  );
}
