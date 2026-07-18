import { useState, useEffect } from "react";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Loader2, CheckCircle2, RefreshCw, Unlink, Eye, EyeOff, AlertCircle, Zap } from "lucide-react";
import { toast } from "sonner";

export default function DexcomShareSetup() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("");
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
      const res = await base44.functions.invoke("dexcomShare", { action: "status" });
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
      if (username || password) {
        await base44.functions.invoke("dexcomShare", { action: "configure", username, password });
        setPassword("");
      }
      const res = await base44.functions.invoke("dexcomShare", { action: "test" });
      const latest = res.data?.latest;
      setMessage(latest ? `Live — ${latest.value} mg/dL, ${latest.trend}` : "Connected (no recent reading — is Share enabled with a follower?)");
    });

  const sync = () =>
    run(async () => {
      const res = await base44.functions.invoke("dexcomShare", { action: "sync" });
      setMessage(`Pulled ${res.data.readings_synced} new readings (${res.data.readings_skipped} already stored)`);
      toast.success(`Dexcom Share: ${res.data.readings_synced} readings`);
    });

  const disconnect = () =>
    run(async () => {
      await base44.functions.invoke("dexcomShare", { action: "disconnect" });
      setUsername("");
      setPassword("");
      setMessage("Disconnected; credentials cleared.");
    });

  const connected = status?.connected;

  return (
    <div className="bg-card rounded-xl border border-border p-5 space-y-4">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 rounded-xl bg-red-500/10 flex items-center justify-center flex-shrink-0">
          <Zap className="w-6 h-6 text-red-500" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <h3 className="font-semibold text-sm">Dexcom Share (real-time)</h3>
            {loading ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin text-muted-foreground" />
            ) : connected ? (
              <span className="text-[10px] px-2 py-0.5 rounded-full font-medium bg-green-100 text-green-700 flex items-center gap-1">
                <CheckCircle2 className="w-3 h-3" /> Live
              </span>
            ) : (
              <span className="text-[10px] px-2 py-0.5 rounded-full font-medium bg-muted text-muted-foreground">
                Not connected
              </span>
            )}
          </div>
          <p className="text-sm text-muted-foreground">
            The follower feed the Dexcom Follow app uses — no delay, unlike the official API's ~1 hour lag. Sign in
            with the Dexcom <em>account</em> (sharer's) username and password. Share must be enabled with at least one
            follower.
          </p>
        </div>
      </div>

      {!connected && (
        <div className="space-y-3">
          <div>
            <Label htmlFor="share-username" className="text-xs">Dexcom account username</Label>
            <Input
              id="share-username"
              placeholder="Username or email"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={busy}
              className="mt-1"
              autoComplete="off"
            />
          </div>
          <div>
            <Label htmlFor="share-password" className="text-xs">Password</Label>
            <div className="relative mt-1">
              <Input
                id="share-password"
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
          <Button size="sm" onClick={saveAndTest} disabled={busy || (!status?.configured && (!username || !password))}>
            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1" /> : null}
            Save &amp; Test
          </Button>
        ) : (
          <>
            <Button size="sm" onClick={sync} disabled={busy} className="gap-2">
              <RefreshCw className={`w-3.5 h-3.5 ${busy ? "animate-spin" : ""}`} /> Sync 24h
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

      {status?.latest_reading && (
        <p className="text-xs text-muted-foreground">
          Latest stored Share reading: {new Date(status.latest_reading).toLocaleString()}
        </p>
      )}
    </div>
  );
}
