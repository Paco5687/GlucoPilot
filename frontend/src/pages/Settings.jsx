import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Settings as SettingsIcon, Loader2, KeyRound, Save, CheckCircle2 } from "lucide-react";
import { toast } from "sonner";
import ProviderAccess from "@/components/settings/ProviderAccess";
import InsuranceSettings from "@/components/settings/InsuranceSettings";
import ProfileSettings from "@/components/settings/ProfileSettings";
import ConditionsSettings from "@/components/settings/ConditionsSettings";
import HypothesesSettings from "@/components/settings/HypothesesSettings";
import MedicationsSettings from "@/components/settings/MedicationsSettings";
import AllergiesSettings from "@/components/settings/AllergiesSettings";

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

function SecretField({ label, name, meta, value, onChange, help }) {
  const placeholder = meta?.configured
    ? `Configured (${meta.hint})${meta.source === "env" ? " — from .env" : ""} — type to replace`
    : "Not set";
  return (
    <div>
      <Label htmlFor={name} className="text-xs flex items-center gap-1.5">
        <KeyRound className="w-3 h-3 text-muted-foreground" /> {label}
        {meta?.configured && <CheckCircle2 className="w-3 h-3 text-green-600" />}
      </Label>
      <Input
        id={name}
        type="password"
        autoComplete="off"
        className="mt-1 font-mono text-xs"
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      {help && <p className="text-[11px] text-muted-foreground mt-1">{help}</p>}
    </div>
  );
}

function Section({ title, description, children }) {
  return (
    <div className="bg-card rounded-xl border border-border p-5 space-y-4">
      <div>
        <h3 className="font-semibold text-sm">{title}</h3>
        {description && <p className="text-xs text-muted-foreground mt-0.5">{description}</p>}
      </div>
      {children}
    </div>
  );
}

export default function Settings() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [values, setValues] = useState({});
  const [secretInputs, setSecretInputs] = useState({});

  useEffect(() => {
    load();
  }, []);

  async function load() {
    setLoading(true);
    try {
      const d = await api("/api/settings");
      setData(d);
      setValues(d.values);
      setSecretInputs({});
    } catch (err) {
      toast.error(err.message);
    }
    setLoading(false);
  }

  async function save() {
    setSaving(true);
    try {
      const secrets = Object.fromEntries(Object.entries(secretInputs).filter(([, v]) => v !== ""));
      const d = await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ values, secrets }),
      });
      setData(d);
      setValues(d.values);
      setSecretInputs({});
      toast.success("Settings saved");
    } catch (err) {
      toast.error(err.message);
    }
    setSaving(false);
  }

  const setValue = (name, v) => setValues((prev) => ({ ...prev, [name]: v }));
  const setSecret = (name, v) => setSecretInputs((prev) => ({ ...prev, [name]: v }));

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground text-sm">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading settings…
      </div>
    );
  }
  if (!data) return null;

  const syncEnabled = String(values.sync_enabled ?? "true").toLowerCase() !== "false";

  return (
    <div className="space-y-6 max-w-2xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold">Settings</h1>
          <p className="text-sm text-muted-foreground mt-1">
            API keys and app options. Values set here are stored in the app database and override the server's .env.
          </p>
        </div>
        <SettingsIcon className="w-6 h-6 text-primary" />
      </div>

      <ProfileSettings />

      <ConditionsSettings />
      <HypothesesSettings />

      <MedicationsSettings />

      <AllergiesSettings />

      <InsuranceSettings />

      <Section
        title="AI"
        description="Powers the AI Analyst, pattern & insight narratives, lab-report reading, and the Visit Report. Pick whichever fits you — a cloud provider is the simplest (just paste an API key), or run a local model for full privacy if you have the hardware."
      >
        <div className="flex gap-2 flex-wrap">
          {[
            { id: "anthropic", label: "Anthropic (Claude)" },
            { id: "openai", label: "OpenAI" },
            { id: "local", label: "Local model" },
          ].map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => setValue("llm_provider", p.id)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                (values.llm_provider || "anthropic") === p.id
                  ? "bg-primary text-primary-foreground border-primary"
                  : "bg-secondary border-border hover:bg-accent"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
        {(values.llm_provider || "anthropic") === "anthropic" && (
          <>
            <SecretField
              label="Anthropic API key"
              name="anthropic_api_key"
              meta={data.secrets.anthropic_api_key}
              value={secretInputs.anthropic_api_key || ""}
              onChange={(v) => setSecret("anthropic_api_key", v)}
              help="Create one at console.anthropic.com → API keys. No GPU needed."
            />
            <div>
              <Label htmlFor="anthropic_model" className="text-xs">Model</Label>
              <Input
                id="anthropic_model"
                className="mt-1 font-mono text-xs"
                value={values.anthropic_model || ""}
                onChange={(e) => setValue("anthropic_model", e.target.value)}
              />
            </div>
          </>
        )}
        {(values.llm_provider || "anthropic") === "openai" && (
          <>
            <SecretField
              label="OpenAI API key"
              name="openai_api_key"
              meta={data.secrets.openai_api_key}
              value={secretInputs.openai_api_key || ""}
              onChange={(v) => setSecret("openai_api_key", v)}
              help="Create one at platform.openai.com → API keys. No GPU needed. Use a vision model (e.g. gpt-4o / gpt-4o-mini) so lab-report reading works."
            />
            <div>
              <Label htmlFor="openai_model" className="text-xs">Model</Label>
              <Input
                id="openai_model"
                className="mt-1 font-mono text-xs"
                placeholder="gpt-4o-mini"
                value={values.openai_model || ""}
                onChange={(e) => setValue("openai_model", e.target.value)}
              />
            </div>
          </>
        )}
        {(values.llm_provider || "anthropic") === "local" && (
          <>
            <div>
              <Label htmlFor="local_llm_url" className="text-xs">Server URL (OpenAI-compatible)</Label>
              <Input
                id="local_llm_url"
                className="mt-1 font-mono text-xs"
                placeholder="unix:///run/glucopilot/llm.sock"
                value={values.local_llm_url || ""}
                onChange={(e) => setValue("local_llm_url", e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="local_llm_model" className="text-xs">Model name</Label>
              <Input
                id="local_llm_model"
                className="mt-1 font-mono text-xs"
                placeholder="qwen3-vl-8b"
                value={values.local_llm_model || ""}
                onChange={(e) => setValue("local_llm_model", e.target.value)}
              />
              <p className="text-[11px] text-muted-foreground mt-1">
                Handles records extraction (vision) and everyday text. Fast, always-on.
              </p>
            </div>
            <div className="border-t border-border pt-3 mt-1 space-y-3">
              <p className="text-xs font-medium">Report model (optional, higher quality)</p>
              <p className="text-[11px] text-muted-foreground -mt-2">
                A larger text model used only for the Visit Report narrative. Loaded on demand and unloaded when idle,
                so it doesn't hold GPU memory the rest of the time. Leave blank to use the model above.
              </p>
              <div>
                <Label htmlFor="quality_llm_url" className="text-xs">Report server URL</Label>
                <Input
                  id="quality_llm_url"
                  className="mt-1 font-mono text-xs"
                  placeholder="unix:///run/glucopilot/ollama.sock"
                  value={values.quality_llm_url || ""}
                  onChange={(e) => setValue("quality_llm_url", e.target.value)}
                />
              </div>
              <div>
                <Label htmlFor="quality_llm_model" className="text-xs">Report model name</Label>
                <Input
                  id="quality_llm_model"
                  className="mt-1 font-mono text-xs"
                  placeholder="gemma3:27b"
                  value={values.quality_llm_model || ""}
                  onChange={(e) => setValue("quality_llm_model", e.target.value)}
                />
              </div>
            </div>
          </>
        )}
      </Section>

      <Section
        title="AI web grounding"
        description="Let the Companion look up general medical facts from trusted sources and cite them, instead of recalling from memory (which it can get wrong). Only the medical topic of your question is sent out — never your records. Off by default."
      >
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            className="h-4 w-4 accent-primary"
            checked={(values.companion_web_grounding || "") === "true"}
            onChange={(e) => setValue("companion_web_grounding", e.target.checked ? "true" : "")}
          />
          Ground Companion answers with trusted sources
        </label>
        <p className="text-xs text-muted-foreground">
          Always uses free NIH sources (MedlinePlus + PubMed, no key needed). Optionally add a web-search
          provider below for broader coverage, restricted to reputable medical domains.
        </p>
        {(values.companion_web_grounding || "") === "true" && (
          <div className="space-y-3 pt-1">
            <div>
              <Label htmlFor="web_search_provider" className="text-xs">Optional web search</Label>
              <select
                id="web_search_provider"
                className="mt-1 w-full h-9 rounded-md border border-border bg-background px-2 text-sm"
                value={values.web_search_provider || ""}
                onChange={(e) => setValue("web_search_provider", e.target.value)}
              >
                <option value="">NIH only (no key)</option>
                <option value="tavily">Tavily</option>
                <option value="brave">Brave Search</option>
              </select>
            </div>
            {(values.web_search_provider === "tavily" || values.web_search_provider === "brave") && (
              <SecretField
                label={`${values.web_search_provider === "tavily" ? "Tavily" : "Brave"} API key`}
                name="web_search_key"
                meta={data.secrets.web_search_key}
                value={secretInputs.web_search_key || ""}
                onChange={(v) => setSecret("web_search_key", v)}
                help={values.web_search_provider === "tavily" ? "From app.tavily.com." : "From api-dashboard.search.brave.com."}
              />
            )}
            <div>
              <Label htmlFor="ncbi_api_key" className="text-xs">NCBI API key (optional)</Label>
              <Input
                id="ncbi_api_key"
                className="mt-1 font-mono text-xs"
                value={values.ncbi_api_key || ""}
                onChange={(e) => setValue("ncbi_api_key", e.target.value)}
                placeholder="Raises PubMed rate limits — ncbi.nlm.nih.gov/account"
              />
            </div>
          </div>
        )}
      </Section>

      <Section
        title="Oura Ring"
        description="OAuth app credentials from cloud.ouraring.com → your OAuth application. Then connect on the Connections page."
      >
        <div>
          <Label htmlFor="oura_client_id" className="text-xs">Client ID</Label>
          <Input
            id="oura_client_id"
            className="mt-1 font-mono text-xs"
            value={values.oura_client_id || ""}
            onChange={(e) => setValue("oura_client_id", e.target.value)}
          />
        </div>
        <SecretField
          label="Client secret"
          name="oura_client_secret"
          meta={data.secrets.oura_client_secret}
          value={secretInputs.oura_client_secret || ""}
          onChange={(v) => setSecret("oura_client_secret", v)}
          help={`Register this redirect URI in the Oura console: ${window.location.origin}/oura-callback`}
        />
      </Section>

      <Section
        title="Fitbit"
        description="Register a free Personal app at dev.fitbit.com. Then connect on the Connections page."
      >
        <div>
          <Label htmlFor="fitbit_client_id" className="text-xs">Client ID</Label>
          <Input
            id="fitbit_client_id"
            className="mt-1 font-mono text-xs"
            value={values.fitbit_client_id || ""}
            onChange={(e) => setValue("fitbit_client_id", e.target.value)}
          />
        </div>
        <SecretField
          label="Client secret"
          name="fitbit_client_secret"
          meta={data.secrets.fitbit_client_secret}
          value={secretInputs.fitbit_client_secret || ""}
          onChange={(v) => setSecret("fitbit_client_secret", v)}
          help={`Register this redirect URI in the Fitbit app settings: ${window.location.origin}/fitbit-callback`}
        />
      </Section>

      <Section
        title="Google Health (Fitbit)"
        description="Fitbit's successor API. Register an app in Google Cloud Console, enable the Google Health API, then connect on the Connections page. Legacy Fitbit Web API retires Sep 30, 2026."
      >
        <div>
          <Label htmlFor="google_health_client_id" className="text-xs">Client ID</Label>
          <Input
            id="google_health_client_id"
            className="mt-1 font-mono text-xs"
            value={values.google_health_client_id || ""}
            onChange={(e) => setValue("google_health_client_id", e.target.value)}
          />
        </div>
        <SecretField
          label="Client secret"
          name="google_health_client_secret"
          meta={data.secrets.google_health_client_secret}
          value={secretInputs.google_health_client_secret || ""}
          onChange={(v) => setSecret("google_health_client_secret", v)}
          help={`Add this as an Authorized redirect URI on the OAuth client: ${window.location.origin}/google-health-callback`}
        />
      </Section>

      <Section
        title="Dexcom"
        description="Developer app credentials from developer.dexcom.com. Production API only — connect from the Connections page when ready."
      >
        <div>
          <Label htmlFor="dexcom_client_id" className="text-xs">Client ID</Label>
          <Input
            id="dexcom_client_id"
            className="mt-1 font-mono text-xs"
            value={values.dexcom_client_id || ""}
            onChange={(e) => setValue("dexcom_client_id", e.target.value)}
          />
        </div>
        <SecretField
          label="Client secret"
          name="dexcom_client_secret"
          meta={data.secrets.dexcom_client_secret}
          value={secretInputs.dexcom_client_secret || ""}
          onChange={(v) => setSecret("dexcom_client_secret", v)}
        />
        <div className="text-[11px] text-muted-foreground space-y-0.5">
          <p>Redirect URI (set in .env, must match the Dexcom portal): <span className="font-mono">{data.readonly.dexcom_redirect_uri || "not set"}</span></p>
          <p>Environment: <span className="font-mono">{data.readonly.dexcom_env || "production_us"}</span></p>
        </div>
      </Section>

      <Section
        title="Bug reports (GitHub)"
        description="The in-app 'Report a bug' button files a GitHub issue. Add a token with issue (and Projects, if used) permissions. Without a token, the button opens a pre-filled issue in the reporter's own GitHub instead."
      >
        <SecretField
          label="GitHub token"
          name="github_token"
          meta={data.secrets.github_token}
          value={secretInputs.github_token || ""}
          onChange={(v) => setSecret("github_token", v)}
          help="Fine-grained token with Issues: read/write (and Projects: read/write to auto-add to a board)."
        />
        <div className="grid sm:grid-cols-2 gap-3">
          <div>
            <Label htmlFor="github_repo" className="text-xs">Repository (owner/name)</Label>
            <Input
              id="github_repo"
              className="mt-1 font-mono text-xs"
              placeholder="Paco5687/GlucoPilot"
              value={values.github_repo || ""}
              onChange={(e) => setValue("github_repo", e.target.value)}
            />
          </div>
          <div>
            <Label htmlFor="github_project_number" className="text-xs">Project number (optional)</Label>
            <Input
              id="github_project_number"
              className="mt-1 font-mono text-xs"
              placeholder="e.g. 3"
              value={values.github_project_number || ""}
              onChange={(e) => setValue("github_project_number", e.target.value)}
            />
          </div>
        </div>
        <p className="text-[11px] text-muted-foreground">
          Project number is in the board's URL (…/projects/<b>N</b>). New issues are added to it automatically.
        </p>
      </Section>

      <ProviderAccess />

      <Section
        title="Phone automation (cycle data)"
        description="Automated cycle imports from a phone: Health Auto Export (Apple Health, scheduled) or an iOS Shortcut sharing the Lively CSV. POST to this endpoint with the bearer token."
      >
        <div className="text-xs space-y-1 font-mono bg-muted/50 rounded-lg p-3 overflow-x-auto">
          <div>URL: <b>{window.location.origin}/api/ingest/cycle</b></div>
          <div>Header: <b>Authorization: Bearer {data.readonly.ingest_token}</b></div>
        </div>
        <p className="text-[11px] text-muted-foreground">
          This token can only write cycle data — it grants no other access. Accepts the Lively CSV export or
          Health Auto Export JSON; existing manual logs are never overwritten.
        </p>
      </Section>

      <Section title="General">
        <div>
          <Label htmlFor="app_timezone" className="text-xs">Timezone</Label>
          <Input
            id="app_timezone"
            className="mt-1 font-mono text-xs"
            placeholder="America/New_York"
            value={values.app_timezone || ""}
            onChange={(e) => setValue("app_timezone", e.target.value)}
          />
          <p className="text-[11px] text-muted-foreground mt-1">Used for pattern analysis (time-of-day bucketing).</p>
        </div>
        <div className="flex items-center justify-between">
          <div>
            <Label className="text-xs">Background sync</Label>
            <p className="text-[11px] text-muted-foreground">Nightscout & Dexcom every 5 min, Tandem hourly, Oura every 6 h.</p>
          </div>
          <Switch
            checked={syncEnabled}
            onCheckedChange={(checked) => setValue("sync_enabled", checked ? "true" : "false")}
          />
        </div>
      </Section>

      <Button onClick={save} disabled={saving} className="gap-2">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
        {saving ? "Saving…" : "Save settings"}
      </Button>
    </div>
  );
}
