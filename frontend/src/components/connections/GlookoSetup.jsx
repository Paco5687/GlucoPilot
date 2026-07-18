import { useState, useEffect } from "react";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Loader2, CheckCircle2, RefreshCw, Unlink, Eye, EyeOff, AlertCircle } from "lucide-react";
import { toast } from "sonner";

export default function GlookoSetup() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [region, setRegion] = useState("us");
  const [showPassword, setShowPassword] = useState(false);
  const [includeCgm, setIncludeCgm] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState(false);

  useEffect(() => {
    refresh();
  }, []);

  async function refresh() {
    setLoading(true);
    try {
      const res = await base44.functions.invoke("glooko", { action: "status" });
      setStatus(res.data);
      if (res.data?.region) setRegion(res.data.region);
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
        await base44.functions.invoke("glooko", { action: "configure", email, password, region });
        setPassword("");
      }
      await base44.functions.invoke("glooko", { action: "test" });
      setMessage("Connected to Glooko.");
    });

  const sync = () =>
    run(async () => {
      const res = await base44.functions.invoke("glooko", { action: "sync" });
      setMessage(`Synced ${res.data.treatments_synced} treatments (${res.data.treatments_skipped} already present)`);
      toast.success(`Glooko: ${res.data.treatments_synced} treatments synced`);
    });

  const backfill = () =>
    run(async () => {
      const res = await base44.functions.invoke("glooko", { action: "backfill", days: 30, include_cgm: includeCgm });
      setMessage(
        `Backfilled ${res.data.treatments_synced} treatments` +
          (includeCgm ? ` and ${res.data.readings_synced} readings` : "") +
          ` (${res.data.treatments_skipped} duplicates skipped)`
      );
      toast.success(`Glooko backfill: ${res.data.treatments_synced} treatments`);
    });

  const disconnect = () =>
    run(async () => {
      await base44.functions.invoke("glooko", { action: "disconnect" });
      setEmail("");
      setPassword("");
      setMessage("Disconnected; credentials cleared.");
    });

  const connected = status?.connected;

  return (
    <div className="bg-card rounded-xl border border-border p-5 space-y-4">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 rounded-xl bg-orange-500/10 flex items-center justify-center flex-shrink-0">
          <span className="text-lg">🗂️</span>
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <h3 className="font-semibold text-sm">Glooko</h3>
            {loading ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin text-muted-foreground" />
            ) : connected ? (
              <span className="text-[10px] px-2 py-0.5 rounded-full font-medium bg-green-100 text-green-700 flex items-center gap-1">
                <CheckCircle2 className="w-3 h-3" /> Connected
              </span>
            ) : (
              <span className="text-[10px] px-2 py-0.5 rounded-full font-medium bg-muted text-muted-foreground">
                Not connected
              </span>
            )}
          </div>
          <p className="text-sm text-muted-foreground">
            Failsafe treatment source: Glooko aggregates pump data from Tandem (linked Tandem Source account) and
            Omnipod 5 (cloud-to-cloud, ~1 h delay). Unofficial API; the account must not have 2FA enabled.
          </p>
        </div>
      </div>

      {!connected && (
        <div className="space-y-3">
          <div>
            <Label htmlFor="glooko-email" className="text-xs">Glooko account email</Label>
            <Input
              id="glooko-email"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={busy}
              className="mt-1"
              autoComplete="off"
            />
          </div>
          <div>
            <Label htmlFor="glooko-password" className="text-xs">Password</Label>
            <div className="relative mt-1">
              <Input
                id="glooko-password"
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
          <div>
            <Label className="text-xs">Region</Label>
            <div className="flex gap-2 mt-1">
              {["us", "eu", "ca"].map((r) => (
                <button
                  key={r}
                  type="button"
                  onClick={() => setRegion(r)}
                  className={`px-3 py-1 rounded-lg text-xs font-medium border transition-colors ${
                    region === r ? "bg-primary text-primary-foreground border-primary" : "bg-secondary border-border hover:bg-accent"
                  }`}
                >
                  {r.toUpperCase()}
                </button>
              ))}
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
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
              <input type="checkbox" checked={includeCgm} onChange={(e) => setIncludeCgm(e.target.checked)} />
              include CGM in backfill
            </label>
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
