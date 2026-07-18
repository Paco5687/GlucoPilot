import { useState, useEffect } from "react";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Moon, RefreshCw, CheckCircle, AlertCircle, Loader2, Eye, EyeOff, Trash2 } from "lucide-react";

export default function NightscoutSetup() {
  const [settings, setSettings] = useState(null);
  const [loading, setLoading] = useState(true);
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const [status, setStatus] = useState("idle");
  const [message, setMessage] = useState("");
  const [syncRange, setSyncRange] = useState(24);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    loadSettings();
  }, []);

  async function loadSettings() {
    const user = await base44.auth.me();
    const list = await base44.entities.UserSettings.filter({ owner_email: user.email }, "-created_date", 1);
    if (list.length) {
      setSettings(list[0]);
      setUrl(list[0].nightscout_url || "");
      setSecret(list[0].nightscout_api_secret || "");
      if (list[0].nightscout_connected) setStatus("connected");
    }
    setLoading(false);
  }

  async function saveAndTest() {
    setSaving(true);
    setMessage("");
    try {
      const user = await base44.auth.me();
      const data = {
        nightscout_url: url.trim(),
        nightscout_api_secret: secret.trim(),
        nightscout_connected: false,
        owner_email: user.email,
      };

      let settingsId;
      if (settings) {
        await base44.entities.UserSettings.update(settings.id, data);
        settingsId = settings.id;
      } else {
        const created = await base44.entities.UserSettings.create(data);
        settingsId = created.id;
      }

      // Test connection
      setStatus("testing");
      const res = await base44.functions.invoke("nightscout", { action: "test" });
      if (res.data?.ok) {
        await base44.entities.UserSettings.update(settingsId, { nightscout_connected: true });
        setStatus("connected");
        setMessage(`Connected to "${res.data.name}"`);
      } else {
        setStatus("error");
        setMessage(res.data?.error || "Could not reach Nightscout");
      }

      // Reload settings
      const updated = await base44.entities.UserSettings.list("-created_date", 1);
      if (updated.length) setSettings(updated[0]);
    } catch (err) {
      setStatus("error");
      setMessage(err?.response?.data?.error || err.message || "Could not reach Nightscout");
    }
    setSaving(false);
  }

  async function syncData() {
    setStatus("syncing");
    setMessage("");
    try {
      const res = await base44.functions.invoke("nightscout", { action: "sync", hours: syncRange });
      if (res.data?.ok) {
        setStatus("connected");
        setMessage(`Synced ${res.data.readings_synced} readings and ${res.data.treatments_synced} treatments`);
        if (settings) {
          await base44.entities.UserSettings.update(settings.id, {
            nightscout_connected: true,
            last_nightscout_sync: new Date().toISOString(),
          });
        }
      } else {
        setStatus("error");
        setMessage(res.data?.error || "Sync failed");
      }
    } catch (err) {
      setStatus("error");
      setMessage(err?.response?.data?.error || err.message || "Sync failed");
    }
  }

  async function disconnect() {
    if (!settings) return;
    await base44.entities.UserSettings.update(settings.id, {
      nightscout_url: "",
      nightscout_api_secret: "",
      nightscout_connected: false,
    });
    setUrl("");
    setSecret("");
    setStatus("idle");
    setMessage("");
    setSettings({ ...settings, nightscout_url: "", nightscout_api_secret: "", nightscout_connected: false });
  }

  const isBusy = status === "testing" || status === "syncing" || saving;
  const isConnected = status === "connected";

  if (loading) {
    return (
      <div className="bg-card rounded-xl border border-border p-5 flex items-center gap-2">
        <Loader2 className="w-4 h-4 animate-spin" />
        <span className="text-sm text-muted-foreground">Loading settings…</span>
      </div>
    );
  }

  return (
    <div className="bg-card rounded-xl border border-border p-5 space-y-4">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 rounded-xl bg-primary/10 flex items-center justify-center flex-shrink-0">
          <Moon className="w-6 h-6 text-primary" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <h3 className="font-semibold text-sm">Nightscout</h3>
            <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
              isConnected ? "bg-green-100 text-green-700" :
              status === "error" ? "bg-red-100 text-red-700" :
              isBusy ? "bg-amber-100 text-amber-700" :
              "bg-muted text-muted-foreground"
            }`}>
              {status === "testing" ? "Testing…" :
               status === "syncing" ? "Syncing…" :
               isConnected ? "Connected" :
               status === "error" ? "Error" : "Not Connected"}
            </span>
          </div>
          <p className="text-sm text-muted-foreground">
            Connect your Nightscout site to sync CGM readings, treatments, and profile data.
          </p>
        </div>
      </div>

      {/* URL & Secret fields */}
      <div className="space-y-3">
        <div>
          <Label htmlFor="ns-url" className="text-xs">Nightscout URL</Label>
          <Input
            id="ns-url"
            placeholder="https://yoursite.up.railway.app"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            disabled={isBusy}
            className="mt-1"
          />
        </div>
        <div>
          <Label htmlFor="ns-secret" className="text-xs">API Secret (optional)</Label>
          <div className="relative mt-1">
            <Input
              id="ns-secret"
              type={showSecret ? "text" : "password"}
              placeholder="Your API secret"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              disabled={isBusy}
              className="pr-10"
            />
            <button
              type="button"
              onClick={() => setShowSecret(!showSecret)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            >
              {showSecret ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
        </div>
      </div>

      {message && (
        <div className={`flex items-center gap-2 text-xs ${status === "error" ? "text-red-600" : "text-green-700"}`}>
          {status === "error" ? <AlertCircle className="w-3.5 h-3.5" /> : <CheckCircle className="w-3.5 h-3.5" />}
          {message}
        </div>
      )}

      {/* Action buttons */}
      <div className="flex items-center gap-3 flex-wrap">
        {!isConnected ? (
          <Button size="sm" onClick={saveAndTest} disabled={isBusy || !url.trim()}>
            {isBusy ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1" /> : null}
            Save & Test
          </Button>
        ) : (
          <>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">Sync range:</span>
              {[6, 24, 48, 168].map((h) => (
                <button
                  key={h}
                  onClick={() => setSyncRange(h)}
                  className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-colors ${
                    syncRange === h ? "bg-primary text-primary-foreground" : "bg-secondary hover:bg-accent"
                  }`}
                >
                  {h === 168 ? "7d" : `${h}h`}
                </button>
              ))}
            </div>
            <Button size="sm" onClick={syncData} disabled={isBusy} className="gap-2 ml-auto">
              <RefreshCw className={`w-3.5 h-3.5 ${isBusy ? "animate-spin" : ""}`} />
              {status === "syncing" ? "Syncing…" : "Sync Now"}
            </Button>
            <Button size="sm" variant="outline" onClick={saveAndTest} disabled={isBusy}>
              Re-test
            </Button>
            <Button size="sm" variant="ghost" onClick={disconnect} disabled={isBusy} className="text-destructive hover:text-destructive">
              <Trash2 className="w-3.5 h-3.5" />
            </Button>
          </>
        )}
      </div>

      {settings?.last_nightscout_sync && (
        <p className="text-xs text-muted-foreground">
          Last synced: {new Date(settings.last_nightscout_sync).toLocaleString()}
        </p>
      )}
    </div>
  );
}