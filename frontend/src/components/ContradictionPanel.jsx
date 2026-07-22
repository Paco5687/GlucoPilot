import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, History, Loader2 } from "lucide-react";
import { useAuth } from "@/lib/AuthContext";
import { toast } from "sonner";

const RESOLUTIONS = [
  ["data_corrected", "Data corrected"],
  ["accepted_left", "Left source accepted"],
  ["accepted_right", "Right source accepted"],
  ["both_valid", "Both are valid in context"],
  ["not_applicable", "Not applicable"],
];

function details(side) {
  if (!side) return [];
  const output = [];
  if (side.name) output.push(side.name);
  if (side.value != null) output.push(`${side.value}${side.unit ? ` ${side.unit}` : ""}`);
  if (side.reference_low != null || side.reference_high != null) {
    output.push(`Range ${side.reference_low ?? "—"}–${side.reference_high ?? "—"}${side.unit ? ` ${side.unit}` : ""}`);
  }
  if (side.expected_cycle_phases?.length) output.push(`Expected: ${side.expected_cycle_phases.join(", ")}`);
  if (side.cycle_phases?.length) output.push(`Recorded: ${side.cycle_phases.join(", ")}`);
  if (side.source) output.push(`Source: ${side.source}`);
  if (side.observed_at) output.push(side.observed_at);
  return output;
}

function EvidenceSide({ side }) {
  return (
    <div className="rounded-md border border-border bg-background/80 p-2">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{side?.label || "Evidence"}</div>
      {details(side).map((line, index) => (
        <div key={`${line}-${index}`} className={index === 0 ? "text-sm font-medium" : "text-xs text-muted-foreground"}>{line}</div>
      ))}
    </div>
  );
}

export default function ContradictionPanel({ domains = [], title = "Data contradictions" }) {
  const { isAdmin } = useAuth();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showResolved, setShowResolved] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [resolutionKind, setResolutionKind] = useState("data_corrected");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const domainKey = useMemo(() => [...domains].sort().join(","), [domains]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({
        domains: domainKey,
        include_resolved: String(showResolved),
        refresh: "true",
      });
      const response = await fetch(`/api/contradictions?${params}`, { credentials: "same-origin" });
      const body = await response.json();
      setItems(response.ok ? body.contradictions || [] : []);
    } catch {
      setItems([]);
    }
    setLoading(false);
  }, [domainKey, showResolved]);

  useEffect(() => { void load(); }, [load]);

  async function resolve(item) {
    setSaving(true);
    try {
      const response = await fetch(`/api/contradictions/${item.id}/resolve`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolution_kind: resolutionKind, note }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || "Resolution failed");
      setEditingId(null);
      setNote("");
      await load();
    } catch (error) {
      toast.error(error.message || "Resolution failed");
    }
    setSaving(false);
  }

  if (loading && !items.length) {
    return <div className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="w-3.5 h-3.5 animate-spin" /> Checking data contradictions…</div>;
  }
  if (!items.length && !showResolved) return null;

  return (
    <section className="rounded-xl border border-amber-200 bg-amber-50/70 p-4 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="font-semibold text-sm flex items-center gap-2"><AlertTriangle className="w-4 h-4 text-amber-700" /> {title}</h2>
          <p className="text-xs text-muted-foreground mt-0.5">Both sides remain visible until an attributed resolution is recorded.</p>
        </div>
        <button onClick={() => setShowResolved((value) => !value)} className="text-xs inline-flex items-center gap-1 text-primary hover:underline">
          <History className="w-3.5 h-3.5" /> {showResolved ? "Hide resolved" : "Show history"}
        </button>
      </div>

      {!items.length && showResolved && <div className="text-xs text-muted-foreground">No contradiction history for this area.</div>}
      {items.map((item) => (
        <article key={item.id} className={`rounded-lg border bg-card p-3 ${item.severity === "blocking" && item.resolution_state === "unresolved" ? "border-red-300" : "border-border"}`}>
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-sm font-medium">{item.explanation}</div>
              <div className="text-[10px] uppercase text-muted-foreground mt-0.5">{item.domain} · {item.severity} · {item.resolution_state}</div>
            </div>
            {item.resolution_state === "resolved" && <CheckCircle2 className="w-4 h-4 text-emerald-600 flex-none" />}
          </div>
          <div className="grid gap-2 sm:grid-cols-2 mt-2">
            <EvidenceSide side={item.left} />
            <EvidenceSide side={item.right} />
          </div>
          {item.detection_state === "not_current" && item.resolution_state === "unresolved" && (
            <p className="text-[11px] text-muted-foreground mt-2">No longer detected in the latest evaluation, but intentionally still unresolved.</p>
          )}

          {item.history?.length > 0 && showResolved && (
            <div className="mt-2 border-t border-border pt-2 space-y-1">
              {item.history.map((event) => (
                <div key={event.id} className="text-[10px] text-muted-foreground">
                  {event.created_at} · {event.action} · {event.actor_name} ({event.actor_role}){event.reason ? ` · ${event.reason}` : ""}
                </div>
              ))}
            </div>
          )}

          {isAdmin && item.resolution_state === "unresolved" && (
            editingId === item.id ? (
              <div className="mt-3 grid gap-2 sm:grid-cols-[220px_1fr_auto]">
                <select value={resolutionKind} onChange={(event) => setResolutionKind(event.target.value)} className="h-9 rounded-md border border-input bg-background px-2 text-xs">
                  {RESOLUTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
                <input value={note} onChange={(event) => setNote(event.target.value)} placeholder={item.severity === "blocking" ? "Resolution note required" : "Resolution note"} className="h-9 rounded-md border border-input bg-background px-3 text-xs" />
                <div className="flex gap-2">
                  <button onClick={() => void resolve(item)} disabled={saving || (item.severity === "blocking" && !note.trim())} className="h-9 rounded-md bg-primary px-3 text-xs text-primary-foreground disabled:opacity-50">Resolve</button>
                  <button onClick={() => { setEditingId(null); setNote(""); }} disabled={saving} className="h-9 rounded-md border border-input px-3 text-xs">Cancel</button>
                </div>
              </div>
            ) : (
              <button onClick={() => setEditingId(item.id)} className="mt-3 h-8 rounded-md border border-input bg-background px-3 text-xs hover:bg-accent">Record resolution</button>
            )
          )}
        </article>
      ))}
    </section>
  );
}
