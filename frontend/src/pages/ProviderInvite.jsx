import { useState, useEffect } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Loader2, Stethoscope, CheckCircle2, XCircle } from "lucide-react";

// Public page a provider lands on from an emailed invite link. The token is
// single-use and expiring; they choose their own username and password here,
// so no credential ever travels by email.

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

function Shell({ children }) {
  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-4">
      <div className="w-full max-w-md bg-card rounded-2xl border border-border p-6 space-y-4">
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-lg bg-primary flex items-center justify-center">
            <span className="text-primary-foreground font-mono font-bold text-sm">GP</span>
          </div>
          <span className="font-semibold tracking-tight">GlucoPilot</span>
        </div>
        {children}
      </div>
    </div>
  );
}

export default function ProviderInvite() {
  const [params] = useSearchParams();
  const token = params.get("token") || "";
  const [status, setStatus] = useState(null); // null loading | {valid,...}
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (!token) { setStatus({ valid: false }); return; }
    api(`/api/provider/invite/${encodeURIComponent(token)}`)
      .then(setStatus)
      .catch(() => setStatus({ valid: false }));
  }, [token]);

  async function accept() {
    setError("");
    if (password !== confirm) { setError("Passwords don't match."); return; }
    setBusy(true);
    try {
      await api("/api/provider/invite/accept", {
        method: "POST",
        body: JSON.stringify({ token, username: username.trim(), password }),
      });
      setDone(true);
    } catch (err) {
      setError(err.message || "Could not create the login.");
    }
    setBusy(false);
  }

  if (status === null) {
    return <Shell><div className="flex justify-center py-8"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div></Shell>;
  }

  if (done) {
    return (
      <Shell>
        <div className="text-center space-y-3 py-4">
          <CheckCircle2 className="w-10 h-10 text-emerald-500 mx-auto" />
          <h1 className="font-semibold">You're set up</h1>
          <p className="text-sm text-muted-foreground">
            Your read-only provider login <span className="font-medium">{username.trim()}</span> is ready.
          </p>
          <Button asChild className="w-full"><Link to="/login">Sign in</Link></Button>
        </div>
      </Shell>
    );
  }

  if (!status.valid) {
    return (
      <Shell>
        <div className="text-center space-y-3 py-4">
          <XCircle className="w-10 h-10 text-destructive mx-auto" />
          <h1 className="font-semibold">This invite link isn't valid</h1>
          <p className="text-sm text-muted-foreground">
            It may have expired or already been used. Ask the account owner to send a new one.
          </p>
        </div>
      </Shell>
    );
  }

  return (
    <Shell>
      <div>
        <h1 className="font-semibold flex items-center gap-2">
          <Stethoscope className="w-4 h-4 text-primary" /> Set up your provider access
        </h1>
        <p className="text-xs text-muted-foreground mt-1">
          You've been invited to a read-only view of a GlucoPilot health record. Choose your own
          username and password — this link works once.
        </p>
      </div>
      {status.at_capacity && (
        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-2">
          All provider seats are currently in use. Ask the account owner to free one, then reload this page.
        </p>
      )}
      <div className="space-y-3">
        <div>
          <Label htmlFor="inv_user" className="text-xs">Username</Label>
          <Input id="inv_user" className="mt-1" value={username} onChange={(e) => setUsername(e.target.value)}
            autoComplete="username" placeholder="dr-smith" />
        </div>
        <div>
          <Label htmlFor="inv_pass" className="text-xs">Password (min 8 characters)</Label>
          <Input id="inv_pass" type="password" className="mt-1" value={password}
            onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        </div>
        <div>
          <Label htmlFor="inv_confirm" className="text-xs">Confirm password</Label>
          <Input id="inv_confirm" type="password" className="mt-1" value={confirm}
            onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <Button onClick={accept} className="w-full"
          disabled={busy || status.at_capacity || username.trim().length < 2 || password.length < 8 || !confirm}>
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : "Create my login"}
        </Button>
      </div>
    </Shell>
  );
}
