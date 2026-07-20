import { useState } from "react";
import { useLocation } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Bug, X, Loader2, CheckCircle2 } from "lucide-react";
import { getTrail } from "@/lib/navTrail";

export default function BugReportModal({ open, onClose }) {
  const location = useLocation();
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");

  if (!open) return null;

  async function submit() {
    if (description.trim().length < 5) {
      setError("Please add a little more detail.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/bug-report", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description,
          context: {
            page: location.pathname,
            trail: getTrail(),
            time: new Date().toISOString(),
            app_url: window.location.origin,
            user_agent: navigator.userAgent,
          },
        }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok || !data?.ok) throw new Error(data?.detail || `Request failed (${res.status})`);
      setResult({ github: data.github });
    } catch (err) {
      setError(err.message || "Could not submit the report.");
    }
    setBusy(false);
  }

  function reset() {
    setDescription("");
    setResult(null);
    setError("");
    onClose();
  }

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={reset} />
      <div className="relative bg-card border border-border rounded-xl shadow-xl w-full max-w-md p-5 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="font-semibold text-sm flex items-center gap-2">
            <Bug className="w-4 h-4 text-primary" /> Report a bug
          </h2>
          <button onClick={reset} className="p-1.5 rounded-lg hover:bg-accent text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>

        {result ? (
          <div className="space-y-3 text-sm">
            <div className="flex items-center gap-2 text-green-600">
              <CheckCircle2 className="w-5 h-5" /> Thanks — your report was sent.
            </div>
            <p className="text-xs text-muted-foreground">It went straight to the maintainer. No account needed.</p>
            <Button size="sm" onClick={reset} className="w-full">Done</Button>
          </div>
        ) : (
          <>
            <p className="text-xs text-muted-foreground">
              What went wrong? We'll include the page you're on, your recent navigation, and the time — but nothing about
              your health data. Please don't paste personal health details; issues may be public.
            </p>
            <textarea
              className="w-full h-28 rounded-lg border border-border bg-background p-3 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-primary/30"
              placeholder="Describe the bug and what you were doing when it happened…"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              autoFocus
            />
            <div className="text-[11px] text-muted-foreground">
              Attaching: <span className="font-mono">{location.pathname}</span> · {getTrail().slice(-3).join(" → ") || "—"}
            </div>
            {error && <p className="text-xs text-red-600">{error}</p>}
            <div className="flex justify-end gap-2">
              <Button size="sm" variant="outline" onClick={reset} disabled={busy}>Cancel</Button>
              <Button size="sm" onClick={submit} disabled={busy} className="gap-2">
                {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Bug className="w-3.5 h-3.5" />}
                Submit report
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
