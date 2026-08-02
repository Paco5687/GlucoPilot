import { useState, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Loader2, Stethoscope, Trash2, Link2, Copy, Check, X } from "lucide-react";
import { toast } from "sonner";

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    ...options,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.detail || `Request failed (${res.status})`);
  return data;
}

function fmtDate(seconds) {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export default function ProviderAccess() {
  const [config, setConfig] = useState(null);
  const [invites, setInvites] = useState([]);
  const [busy, setBusy] = useState(false);
  const [inviteUrl, setInviteUrl] = useState("");
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try {
      const [c, inv] = await Promise.all([
        api("/api/provider/config"),
        api("/api/provider/invites"),
      ]);
      setConfig(c);
      setInvites(inv.invites || []);
    } catch { /* settings page handles auth */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function generateInvite() {
    setBusy(true);
    try {
      const r = await api("/api/provider/invites", { method: "POST" });
      // The raw token exists only in this response — compose the link now.
      setInviteUrl(`${window.location.origin}/provider-invite?token=${encodeURIComponent(r.token)}`);
      setCopied(false);
      await load();
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  async function copyInvite() {
    try {
      await navigator.clipboard.writeText(inviteUrl);
      setCopied(true);
      toast.success("Invite link copied — email it to your provider");
    } catch {
      toast.error("Couldn't copy — select the link text manually");
    }
  }

  async function revokeInvite(id) {
    try {
      const r = await api(`/api/provider/invites/${id}`, { method: "DELETE" });
      setInvites(r.invites || []);
      toast.success("Invite revoked");
    } catch (err) {
      toast.error(err.message);
    }
  }

  async function resetPassword(u) {
    const pw = window.prompt(`New password for "${u}" (min 8 characters):`);
    if (!pw) return;
    try {
      await api("/api/provider/config", { method: "POST", body: JSON.stringify({ username: u, password: pw }) });
      toast.success(`Password updated for ${u}`);
    } catch (err) {
      toast.error(err.message);
    }
  }

  async function remove(u) {
    if (!window.confirm(`Remove provider login "${u}"?`)) return;
    try {
      const c = await api("/api/provider/config", { method: "POST", body: JSON.stringify({ username: u, remove: true }) });
      setConfig(c);
      toast.success(`Removed ${u}`);
    } catch (err) {
      toast.error(err.message);
    }
  }

  if (!config) return null;
  const atMax = config.providers.length >= config.max;

  return (
    <div className="bg-card rounded-xl border border-border p-5 space-y-4">
      <div>
        <h3 className="font-semibold text-sm flex items-center gap-2">
          <Stethoscope className="w-4 h-4 text-primary" /> Provider access
        </h3>
        <p className="text-xs text-muted-foreground mt-0.5">
          Up to {config.max} read-only logins for your care team. Generate an invite link and email it —
          your provider picks their own username and password, so no credential ever travels by email.
          Each link works once and expires after 7 days.
        </p>
      </div>

      {config.providers.length > 0 && (
        <div className="space-y-2">
          {config.providers.map((p) => (
            <div key={p.username} className="flex items-center justify-between bg-muted/40 rounded-lg px-3 py-2">
              <span className="text-sm font-medium flex items-center gap-2">
                <Stethoscope className="w-3.5 h-3.5 text-muted-foreground" /> {p.username}
              </span>
              <div className="flex items-center gap-1">
                <Button variant="ghost" size="sm" onClick={() => resetPassword(p.username)} className="text-xs h-7">
                  Reset password
                </Button>
                <button onClick={() => remove(p.username)} className="p-1.5 rounded-lg hover:bg-accent text-destructive" title="Remove">
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {invites.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-[11px] font-medium text-muted-foreground">Pending invites</p>
          {invites.map((inv) => (
            <div key={inv.id} className="flex items-center justify-between bg-muted/30 rounded-lg px-3 py-1.5 text-xs">
              <span className="text-muted-foreground">
                <Link2 className="w-3 h-3 inline mr-1.5" />
                Created {fmtDate(inv.created_at)} · expires {fmtDate(inv.expires_at)}
              </span>
              <button onClick={() => revokeInvite(inv.id)} className="p-1 rounded hover:bg-accent text-destructive" title="Revoke invite">
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}

      {inviteUrl && (
        <div className="rounded-lg border border-primary/30 bg-primary/5 p-3 space-y-2">
          <p className="text-xs font-medium">Invite link — copy it now, it won't be shown again:</p>
          <div className="flex items-center gap-2">
            <code className="text-[11px] bg-background border border-border rounded px-2 py-1.5 flex-1 min-w-0 truncate">{inviteUrl}</code>
            <Button size="sm" variant="outline" onClick={copyInvite} className="gap-1.5 text-xs shrink-0">
              {copied ? <Check className="w-3.5 h-3.5 text-emerald-500" /> : <Copy className="w-3.5 h-3.5" />}
              {copied ? "Copied" : "Copy"}
            </Button>
          </div>
        </div>
      )}

      {atMax ? (
        <p className="text-xs text-muted-foreground">Maximum of {config.max} provider logins reached.</p>
      ) : (
        <Button size="sm" onClick={generateInvite} disabled={busy} className="gap-2 w-fit">
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Link2 className="w-3.5 h-3.5" />}
          Generate invite link
        </Button>
      )}
    </div>
  );
}
