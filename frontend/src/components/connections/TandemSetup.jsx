import { useState, useEffect } from "react";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Loader2, CheckCircle2, RefreshCw, Unlink, Eye, EyeOff, AlertCircle } from "lucide-react";
import { toast } from "sonner";

export default function TandemSetup() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState(false);

  useEffect(() => {
    refresh();
  }, []);

  async function refresh() {
    setLoading(true);
    try {
      const res = await base44.functions.invoke("tandem", { action: "status" });
      setStatus(res.data);
    } catch {
      setStatus(null);
    }
    setLoading(false);
  }

  async function run(fn) {
    setBusy(true);
    setMessage("");
    setError(false);
    try {
      await fn();
    } catch (err) {
      setError(true);
      setMessage(err?.response?.data?.error || err.message || "Request failed");
    }
    setBusy(false);
    refresh();
  }

  const saveAndTest = () =>
    run(async () => {
      if (email || password) {
        await base44.functions.invoke("tandem", { action: "configure", email, password });
        setPassword("");
      }
      const res = await base44.functions.invoke("tandem", { action: "test" });
      setMessage(`Connected — pump serial ${res.data.pump_serial}, last upload ${res.data.last_seen || "unknown"}`);
    });

  const sync = () =>
    run(async () => {
      const res = await base44.functions.invoke("tandem", { action: "sync" });
      setMessage(`Synced ${res.data.treatments_synced} treatments (${res.data.duplicates_skipped} already present)`);
      toast.success(`Tandem: ${res.data.treatments_synced} treatments synced`);
    });

  const backfill = () =>
    run(async () => {
      const res = await base44.functions.invoke("tandem", { action: "backfill", days: 30 });
      setMessage(`Backfilled ${res.data.treatments_synced} treatments (${res.data.duplicates_skipped} already present)`);
      toast.success(`Tandem backfill: ${res.data.treatments_synced} treatments`);
    });

  const disconnect = () =>
    run(async () => {
      await base44.functions.invoke("tandem", { action: "disconnect" });
      setEmail("");
      setPassword("");
      setMessage("Disconnected; credentials cleared.");
    });

  const connected = status?.connected;

  return (
    <div className="bg-card rounded-xl border border-border p-5 space-y-4">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 rounded-xl bg-sky-500/10 flex items-center justify-center flex-shrink-0">
          <span className="text-lg">💉</span>
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <h3 className="font-semibold text-sm">Tandem Source (pump)</h3>
            {loading ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin text-muted-foreground" />
            ) : connected ? (
              <span className="text-[10px] px-2 py-0.5 rounded-full font-medium bg-green-100 text-green-700 flex items-center gap-1">
                <CheckCircle2 className="w-3 h-3" /> Connected{status?.pump_serial ? ` · SN ${status.pump_serial}` : ""}
              </span>
            ) : (
              <span className="text-[10px] px-2 py-0.5 rounded-full font-medium bg-muted text-muted-foreground">
                Not connected
              </span>
            )}
          </div>
          <p className="text-sm text-muted-foreground">
            Syncs boluses, basal, and suspends from a t:slim X2 or Mobi via your Tandem Source (t:connect) account.
            Uses an unofficial API — if Tandem changes it, this may need an update.
          </p>
        </div>
      </div>

      {!connected && (
        <div className="space-y-3">
          <div>
            <Label htmlFor="tandem-email" className="text-xs">Tandem Source account email</Label>
            <Input
              id="tandem-email"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={busy}
              className="mt-1"
              autoComplete="off"
            />
          </div>
          <div>
            <Label htmlFor="tandem-password" className="text-xs">Password</Label>
            <div className="relative mt-1">
              <Input
                id="tandem-password"
                type={showPassword ? "text" : "password"}
                placeholder={status?.configured ? "Saved — type to replace" : "Account password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={busy}
                className="pr-10"
                autoComplete="off"
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              >
                {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>
        </div>
      )}

      {message && (
        <div className={`flex items-center gap-2 text-xs ${error ? "text-red-600" : "text-green-700"}`}>
          {error ? <AlertCircle className="w-3.5 h-3.5" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
          {message}
        </div>
      )}

      <div className="flex items-center gap-3 flex-wrap">
        {!connected ? (
          <Button size="sm" onClick={saveAndTest} disabled={busy || (!status?.configured && (!email || !password))}>
            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1" /> : null}
            Save &amp; Test
          </Button>
        ) : (
          <>
            <Button size="sm" onClick={sync} disabled={busy} className="gap-2">
              <RefreshCw className={`w-3.5 h-3.5 ${busy ? "animate-spin" : ""}`} /> Sync Now
            </Button>
            <Button size="sm" variant="outline" onClick={backfill} disabled={busy} className="gap-2">
              <RefreshCw className="w-3.5 h-3.5" /> Backfill 30 days
            </Button>
            <Button size="sm" variant="outline" onClick={saveAndTest} disabled={busy}>
              Re-test
            </Button>
            <Button size="sm" variant="ghost" onClick={disconnect} disabled={busy} className="text-destructive hover:text-destructive">
              <Unlink className="w-3.5 h-3.5" />
            </Button>
          </>
        )}
      </div>

      {status?.last_sync && (
        <p className="text-xs text-muted-foreground">Last synced: {new Date(status.last_sync).toLocaleString()}</p>
      )}
    </div>
  );
}
