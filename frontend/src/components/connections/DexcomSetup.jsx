import { useState, useEffect } from "react";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Loader2, CheckCircle2, ExternalLink, Unlink, RefreshCw, AlertTriangle } from "lucide-react";
import { toast } from "sonner";

export default function DexcomSetup() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [backfilling, setBackfilling] = useState(false);

  useEffect(() => {
    refresh();
  }, []);

  async function refresh() {
    setLoading(true);
    try {
      const res = await base44.functions.invoke("dexcom", { action: "status" });
      setStatus(res.data);
    } catch {
      setStatus(null);
    }
    setLoading(false);
  }

  function handleConnect() {
    // Server-side OAuth flow; production Dexcom account only.
    window.location.href = "/dexcom/login";
  }

  async function handleSync() {
    setSyncing(true);
    try {
      const res = await base44.functions.invoke("dexcom", { action: "sync" });
      toast.success(`Dexcom sync complete — ${res.data.readings_synced} readings, ${res.data.events_synced} events`);
    } catch (err) {
      toast.error(err.message || "Dexcom sync failed");
    }
    setSyncing(false);
    refresh();
  }

  async function handleBackfill() {
    setBackfilling(true);
    try {
      const res = await base44.functions.invoke("dexcom", { action: "backfill", days: 30 });
      toast.success(`Backfill complete — ${res.data.readings_synced} readings, ${res.data.events_synced} events`);
    } catch (err) {
      toast.error(err.message || "Dexcom backfill failed");
    }
    setBackfilling(false);
    refresh();
  }

  async function handleDisconnect() {
    await base44.functions.invoke("dexcom", { action: "disconnect" });
    refresh();
  }

  const connected = status?.connected;
  const configured = status?.configured;

  return (
    <div className="bg-card rounded-xl border border-border p-5">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 rounded-xl bg-emerald-500/10 flex items-center justify-center flex-shrink-0">
          <span className="text-lg">🩸</span>
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <h3 className="font-semibold text-sm">Dexcom CGM</h3>
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
          <p className="text-sm text-muted-foreground mb-2">
            Connect your Dexcom account to sync glucose readings (EGVs) and logged events directly from the Dexcom API.
          </p>
          {!loading && !configured && (
            <p className="text-xs text-amber-600 flex items-center gap-1 mb-3">
              <AlertTriangle className="w-3.5 h-3.5" />
              Dexcom client credentials are not configured on the server (.env).
            </p>
          )}
          {!loading && !connected && configured && (
            <p className="text-xs text-muted-foreground mb-3">
              Uses the production Dexcom API ({status?.env}). Connecting authorizes this app for your real Dexcom
              account — only do this when you're ready, as the app has a limited number of production user slots.
            </p>
          )}
          {connected && status?.last_sync && (
            <p className="text-xs text-muted-foreground mb-3">Last sync: {new Date(status.last_sync).toLocaleString()}</p>
          )}
          {!loading && (
            connected ? (
              <div className="flex flex-wrap gap-2">
                <Button size="sm" onClick={handleSync} disabled={syncing || backfilling} className="gap-2">
                  {syncing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                  {syncing ? "Syncing..." : "Sync now"}
                </Button>
                <Button variant="outline" size="sm" onClick={handleBackfill} disabled={syncing || backfilling} className="gap-2">
                  {backfilling ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                  {backfilling ? "Backfilling..." : "Backfill 30 days"}
                </Button>
                <Button variant="outline" size="sm" onClick={handleDisconnect} className="gap-2">
                  <Unlink className="w-3.5 h-3.5" /> Disconnect
                </Button>
              </div>
            ) : (
              <Button size="sm" onClick={handleConnect} disabled={!configured} className="gap-2">
                <ExternalLink className="w-3.5 h-3.5" /> Connect Dexcom
              </Button>
            )
          )}
        </div>
      </div>
    </div>
  );
}
