import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Loader2, Stethoscope, Trash2, UserPlus } from "lucide-react";
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

export default function ProviderAccess() {
  const [config, setConfig] = useState(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api("/api/provider/config").then(setConfig).catch(() => {});
  }, []);

  async function add() {
    setBusy(true);
    try {
      const c = await api("/api/provider/config", {
        method: "POST",
        body: JSON.stringify({ username: username.trim(), password }),
      });
      setConfig(c);
      setUsername("");
      setPassword("");
      toast.success("Provider login added");
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
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
          <Stethoscope className="w-4 h-4 text-primary" /> Provider logins
        </h3>
        <p className="text-xs text-muted-foreground mt-0.5">
          Up to {config.max} read-only logins to share with doctors. Each can view every page and print the Visit
          Report, but cannot change data, settings, or connections. Share the login URL{" "}
          <span className="font-mono">{window.location.origin}/login</span>.
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

      {atMax ? (
        <p className="text-xs text-muted-foreground">Maximum of {config.max} provider logins reached.</p>
      ) : (
        <div className="grid sm:grid-cols-2 gap-3 items-end">
          <div>
            <Label htmlFor="new_prov_user" className="text-xs">New provider username</Label>
            <Input id="new_prov_user" className="mt-1" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" placeholder="dr-smith" />
          </div>
          <div>
            <Label htmlFor="new_prov_pass" className="text-xs">Password (min 8)</Label>
            <Input id="new_prov_pass" type="password" className="mt-1" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
          </div>
          <Button size="sm" onClick={add} disabled={busy || !username.trim() || password.length < 8} className="gap-2 sm:col-span-2 w-fit">
            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <UserPlus className="w-3.5 h-3.5" />}
            Add provider login
          </Button>
        </div>
      )}
    </div>
  );
}
