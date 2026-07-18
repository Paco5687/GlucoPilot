import { useState, useEffect } from "react";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Loader2, CheckCircle2, ExternalLink, Unlink, RefreshCw, Watch } from "lucide-react";
import { toast } from "sonner";

const SCOPES = "activity heartrate oxygen_saturation respiratory_rate sleep temperature";

export default function FitbitSetup() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [clientId, setClientId] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    refresh();
    base44.functions.invoke("fitbit", { action: "get_client_id" }).then((res) => setClientId(res.data.client_id));
  }, []);

  async function refresh() {
    setLoading(true);
    try {
      const res = await base44.functions.invoke("fitbit", { action: "status" });
      setStatus(res.data);
    } catch {
      setStatus(null);
    }
    setLoading(false);
  }

  function handleConnect() {
    const redirectUri = encodeURIComponent(window.location.origin + "/fitbit-callback");
    const url = `https://www.fitbit.com/oauth2/authorize?response_type=code&client_id=${clientId}&redirect_uri=${redirectUri}&scope=${encodeURIComponent(SCOPES)}`;
    const popup = window.open(url, "_blank", "width=600,height=750");
    const timer = setInterval(() => {
      if (!popup || popup.closed) {
        clearInterval(timer);
        refresh();
      }
    }, 500);
  }

  async function handleSync(days) {
    setBusy(true);
    try {
      const res = await base44.functions.invoke("fitbit", { action: "sync", days });
      toast.success(`Fitbit: ${res.data.days_synced} days synced (${res.data.created} new)`);
    } catch (err) {
      toast.error(err?.response?.data?.error || err.message || "Sync failed");
    }
    setBusy(false);
    refresh();
  }

  async function handleDisconnect() {
    await base44.functions.invoke("fitbit", { action: "disconnect" });
    refresh();
  }

  const connected = status?.connected;

  return (
    <div className="bg-card rounded-xl border border-border p-5">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 rounded-xl bg-teal-500/10 flex items-center justify-center flex-shrink-0">
          <Watch className="w-6 h-6 text-teal-500" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <h3 className="font-semibold text-sm">Fitbit</h3>
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
          <p className="text-sm text-muted-foreground mb-1">
            Daily steps, active minutes, resting heart rate, sleep, SpO2, breathing rate, and skin temperature.
          </p>
          {!connected && (
            <p className="text-xs text-muted-foreground mb-3">
              Register a free <b>Personal</b> app at dev.fitbit.com with redirect URI{" "}
              <code className="font-mono bg-muted px-1 py-0.5 rounded">{window.location.origin}/fitbit-callback</code>,
              then add the client ID + secret on the <a href="/settings" className="underline">Settings page</a>.
            </p>
          )}
          {connected && status?.latest_day && (
            <p className="text-xs text-muted-foreground mb-3">Latest day: {status.latest_day}</p>
          )}
          {!loading && (
            connected ? (
              <div className="flex flex-wrap gap-2">
                <Button size="sm" onClick={() => handleSync(7)} disabled={busy} className="gap-2">
                  <RefreshCw className={`w-3.5 h-3.5 ${busy ? "animate-spin" : ""}`} /> Sync now
                </Button>
                <Button variant="outline" size="sm" onClick={() => handleSync(365)} disabled={busy} className="gap-2">
                  <RefreshCw className="w-3.5 h-3.5" /> Backfill 1 year
                </Button>
                <Button variant="outline" size="sm" onClick={handleDisconnect} className="gap-2">
                  <Unlink className="w-3.5 h-3.5" /> Disconnect
                </Button>
              </div>
            ) : (
              <Button size="sm" onClick={handleConnect} disabled={!clientId} className="gap-2">
                <ExternalLink className="w-3.5 h-3.5" /> Connect Fitbit
              </Button>
            )
          )}
        </div>
      </div>
    </div>
  );
}
