import { useState, useEffect } from "react";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Loader2, CheckCircle2, ExternalLink, Unlink } from "lucide-react";

export default function OuraSetup() {
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [connecting, setConnecting] = useState(false);
  const [clientId, setClientId] = useState(null);

  useEffect(() => {
    checkStatus();
    base44.functions.invoke("ouraAuth", { action: "get_client_id" }).then(res => setClientId(res.data.client_id));
  }, []);

  async function checkStatus() {
    setLoading(true);
    const res = await base44.functions.invoke("ouraAuth", { action: "status" });
    setConnected(res.data?.connected || false);
    setLoading(false);
  }

  async function handleConnect() {
    setConnecting(true);
    const user = await base44.auth.me();
    const state = btoa(user.email);
    // Must exactly match the redirect URI registered in the Oura developer console.
    const redirectUri = encodeURIComponent(window.location.origin + "/oura-callback");
    const scopes = "daily+heartrate+workout+session+spo2";
    const authUrl = `https://cloud.ouraring.com/oauth/authorize?response_type=code&client_id=${clientId}&redirect_uri=${redirectUri}&scope=${scopes}&state=${state}`;
    const popup = window.open(authUrl, "_blank", "width=600,height=700");
    const timer = setInterval(() => {
      if (!popup || popup.closed) {
        clearInterval(timer);
        setConnecting(false);
        checkStatus();
      }
    }, 500);
  }

  async function handleDisconnect() {
    await base44.functions.invoke("ouraAuth", { action: "disconnect" });
    setConnected(false);
  }

  return (
    <div className="bg-card rounded-xl border border-border p-5">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 rounded-xl bg-purple-500/10 flex items-center justify-center flex-shrink-0">
          <span className="text-lg">💍</span>
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <h3 className="font-semibold text-sm">Oura Ring</h3>
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
            Connect your Oura Ring to sync sleep, readiness, heart rate, and activity data.
          </p>
          <p className="text-xs text-muted-foreground mb-3">
            Requires this exact redirect URI in your Oura app settings (cloud.ouraring.com → your application):{" "}
            <code className="font-mono bg-muted px-1 py-0.5 rounded">{window.location.origin}/oura-callback</code>
          </p>
          {!loading && (
            connected ? (
              <Button variant="outline" size="sm" onClick={handleDisconnect} className="gap-2">
                <Unlink className="w-3.5 h-3.5" /> Disconnect
              </Button>
            ) : (
              <div className="space-y-2">
                {!clientId && (
                  <p className="text-xs text-amber-600">
                    Add your Oura client ID and secret on the <a href="/settings" className="underline">Settings page</a> first.
                  </p>
                )}
                <Button size="sm" onClick={handleConnect} disabled={connecting || !clientId} className="gap-2">
                  {connecting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ExternalLink className="w-3.5 h-3.5" />}
                  {connecting ? "Connecting..." : "Connect Oura"}
                </Button>
              </div>
            )
          )}
        </div>
      </div>
    </div>
  );
}