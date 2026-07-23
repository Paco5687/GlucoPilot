import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { base44 } from "@/api/base44Client";
import { ExternalLink, FileSearch, Loader2, ShieldAlert, X } from "lucide-react";

const ROLE_LABELS = {
  supporting: "Supporting evidence",
  opposing: "Opposing evidence",
  limiting: "Limitations",
};

function humanize(value) {
  return String(value || "source observations").replaceAll("_", " ");
}

function Limitation({ item }) {
  const missingRate = item.missingness?.missing_rate;
  const detail = item.limitations?.join(" ")
    || (missingRate != null ? `Missingness: ${Math.round(missingRate * 100)}%.` : "")
    || item.discovery_status
    || "This evidence has a recorded limitation.";
  return (
    <li className="rounded-lg border border-amber-200 bg-amber-50/60 p-3 text-xs text-amber-900">
      <span className="font-medium capitalize">{humanize(item.domain || item.kind)}</span>
      {detail && <span className="ml-1">— {detail}</span>}
    </li>
  );
}

function Window({ item }) {
  return (
    <li className="rounded-lg border border-border p-3 space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-medium">{humanize(item.entity_type)}</div>
          <div className="text-xs text-muted-foreground">
            {item.observation_count.toLocaleString()} observations · {item.status}
          </div>
          {item.rationale && <p className="text-xs text-muted-foreground mt-1">{item.rationale}</p>}
        </div>
        <a href={item.href} target="_blank" rel="noreferrer" className="text-xs text-primary inline-flex items-center gap-1">
          Open window <ExternalLink className="w-3 h-3" />
        </a>
      </div>
      {item.source_preview?.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {item.source_preview.map((source) => (
            <a
              key={`${source.entity_type}:${source.entity_id}`}
              href={source.href}
              target="_blank"
              rel="noreferrer"
              className="text-[11px] rounded-full border border-border px-2 py-1 hover:bg-muted"
            >
              Open {humanize(source.entity_type)} <ExternalLink className="w-2.5 h-2.5 inline" />
            </a>
          ))}
          {item.source_preview_truncated && (
            <span className="text-[11px] text-muted-foreground self-center">More in the evidence window</span>
          )}
        </div>
      )}
    </li>
  );
}

export default function ClaimEvidenceDialog({ claimType, claimId }) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState({ loading: false, data: null, error: null });

  useEffect(() => {
    if (!open || state.data) return undefined;
    let current = true;
    setState({ loading: true, data: null, error: null });
    base44.evidence.claim(claimType, claimId).then(
      (data) => { if (current) setState({ loading: false, data, error: null }); },
      (error) => { if (current) setState({ loading: false, data: null, error }); },
    );
    return () => { current = false; };
  }, [claimId, claimType, open, state.data]);

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button type="button" className="h-7 px-2 gap-1.5 text-xs inline-flex items-center justify-center rounded-md font-medium transition-colors hover:bg-accent hover:text-accent-foreground">
          <FileSearch className="w-3.5 h-3.5" /> Show evidence
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/80 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 grid w-full max-w-2xl max-h-[85vh] -translate-x-1/2 -translate-y-1/2 gap-4 overflow-y-auto border bg-background p-6 shadow-lg sm:rounded-lg">
          <div className="flex flex-col space-y-1.5 text-center sm:text-left">
            <Dialog.Title className="text-lg font-semibold leading-none tracking-tight">Evidence and claim history</Dialog.Title>
            <Dialog.Description className="text-sm text-muted-foreground">
              Reproducible source observations, uncertainty, and prior versions for this claim.
            </Dialog.Description>
          </div>
        {state.loading && (
          <div className="h-28 flex items-center justify-center"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>
        )}
        {state.error && (
          <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive flex gap-2">
            <ShieldAlert className="w-4 h-4 mt-0.5" /> Evidence could not be loaded.
          </div>
        )}
        {state.data && (
          <div className="space-y-5">
            <div className="rounded-lg bg-muted/50 p-3 text-xs grid grid-cols-2 gap-2">
              <div><span className="text-muted-foreground">Claim version</span><br />v{state.data.claim.version_number}</div>
              <div><span className="text-muted-foreground">Status</span><br /><span className="capitalize">{state.data.claim.assertion_status}</span></div>
              <div><span className="text-muted-foreground">Algorithm</span><br />{state.data.claim.algorithm.id} {state.data.claim.algorithm.version}</div>
              <div><span className="text-muted-foreground">Evidence set</span><br />{state.data.evidence_set.status}</div>
            </div>
            {Object.entries(ROLE_LABELS).map(([role, label]) => {
              const items = state.data.evidence[role] || [];
              if (items.length === 0) return null;
              return (
                <section key={role} className="space-y-2">
                  <h3 className="text-xs uppercase tracking-wider font-medium text-muted-foreground">{label}</h3>
                  <ul className="space-y-2">
                    {items.map((item, index) => item.window_id
                      ? <Window key={item.window_id} item={item} />
                      : <Limitation key={`${item.kind}:${item.domain || index}`} item={item} />)}
                  </ul>
                </section>
              );
            })}
            {state.data.lineage.length > 1 && (
              <section className="space-y-2">
                <h3 className="text-xs uppercase tracking-wider font-medium text-muted-foreground">Claim history</h3>
                <ul className="space-y-1 text-xs">
                  {state.data.lineage.map((version) => (
                    <li key={version.claim_version_id} className="flex justify-between gap-3 rounded border border-border px-3 py-2">
                      <span>Version {version.version_number}</span>
                      <span className="capitalize text-muted-foreground">{version.assertion_status}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </div>
        )}
          <Dialog.Close className="absolute right-4 top-4 rounded-sm opacity-70 transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2">
            <X className="h-4 w-4" /><span className="sr-only">Close</span>
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
