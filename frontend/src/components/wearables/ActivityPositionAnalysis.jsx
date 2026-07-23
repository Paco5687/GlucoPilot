import { useCallback, useEffect, useMemo, useState } from "react";
import { Activity, Loader2, Plus, RefreshCw } from "lucide-react";
import { toast } from "sonner";

/** @param {import("react").ButtonHTMLAttributes<HTMLButtonElement> & {variant?: string, size?: string}} props */
function Button({ className = "", variant = "default", size = "default", ...props }) {
  const tone = variant === "outline"
    ? "border border-input bg-transparent hover:bg-accent"
    : "bg-primary text-primary-foreground hover:bg-primary/90";
  const dimensions = size === "sm" ? "h-8 px-3 text-xs" : "h-9 px-4 text-sm";
  return (
    <button
      className={`inline-flex items-center justify-center rounded-md font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 ${tone} ${dimensions} ${className}`}
      {...props}
    />
  );
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    ...options,
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.detail || `Request failed (${response.status})`);
  return data;
}

function localInput(date) {
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function initialForm() {
  const end = new Date();
  const start = new Date(end.getTime() - 30 * 60_000);
  return {
    start_time: localInput(start),
    end_time: localInput(end),
    activity: "unknown",
    position: "sitting",
    notes: "",
  };
}

const METRICS = {
  glucose_slope_mg_dl_per_hour: "Glucose slope",
  morning_glucose_slope_mg_dl_per_hour: "Morning glucose slope",
  bolus_response_mg_dl_per_unit: "Observed bolus response",
  cgm_minus_fingerstick_mg_dl: "CGM − fingerstick",
};

function formatTimestamp(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

export default function ActivityPositionAnalysis({ days = 90, readOnly = false }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState(initialForm);

  const load = useCallback(async () => {
    try {
      const result = await api(`/api/activity-position?days=${days}`);
      setData(result);
    } catch (error) {
      toast.error(error.message);
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    load();
  }, [load]);

  const effects = useMemo(
    () => data?.effects || [],
    [data],
  );

  async function createInterval() {
    if (saving) return;
    setSaving(true);
    try {
      await api("/api/activity-position/intervals", {
        method: "POST",
        body: JSON.stringify({
          ...form,
          start_time: new Date(form.start_time).toISOString(),
          end_time: new Date(form.end_time).toISOString(),
        }),
      });
      setForm(initialForm());
      await load();
      toast.success("Activity/position interval recorded");
    } catch (error) {
      toast.error(error.message);
    } finally {
      setSaving(false);
    }
  }

  async function correctInterval(interval) {
    const start = window.prompt(
      "Corrected start (timezone-aware ISO timestamp)",
      interval.start_time,
    );
    if (!start) return;
    const end = window.prompt(
      "Corrected end (timezone-aware ISO timestamp)",
      interval.end_time,
    );
    if (!end) return;
    const activity = window.prompt(
      "Corrected activity (resting, walking, other, unknown)",
      interval.activity,
    );
    if (!activity) return;
    const position = window.prompt(
      "Corrected position (sitting, standing, lying, upright, unknown)",
      interval.position,
    );
    if (!position) return;
    const reason = window.prompt("Why is this interval being corrected?");
    if (!reason?.trim()) return;
    try {
      await api(`/api/activity-position/intervals/${encodeURIComponent(interval.id)}/corrections`, {
        method: "POST",
        body: JSON.stringify({
          start_time: start,
          end_time: end,
          activity,
          position,
          notes: interval.notes || "",
          reason,
        }),
      });
      await load();
      toast.success("Correction appended; the original interval was retained");
    } catch (error) {
      toast.error(error.message);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading activity and position analysis…
      </div>
    );
  }

  return (
    <section className="space-y-3" aria-labelledby="activity-position-heading">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 id="activity-position-heading" className="font-semibold flex items-center gap-2">
            <Activity className="h-4 w-4 text-primary" /> Activity &amp; position
          </h2>
          <p className="text-xs text-muted-foreground mt-1 max-w-3xl">
            Timestamped intervals can be compared with glucose slope, morning response,
            clean bolus-response windows, and CGM/fingerstick differences. These are temporal
            associations—not evidence that position or activity caused a response.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={load} className="gap-1.5">
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </Button>
      </div>

      {!readOnly && data?.can_edit && (
        <div className="rounded-xl border border-border bg-card p-4 space-y-3">
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-6 gap-2">
            <label className="text-xs lg:col-span-2">
              Start
              <input
                aria-label="Activity interval start"
                type="datetime-local"
                value={form.start_time}
                onChange={(event) => setForm({ ...form, start_time: event.target.value })}
                className="mt-1 h-9 w-full rounded-md border border-input bg-transparent px-3"
              />
            </label>
            <label className="text-xs lg:col-span-2">
              End
              <input
                aria-label="Activity interval end"
                type="datetime-local"
                value={form.end_time}
                onChange={(event) => setForm({ ...form, end_time: event.target.value })}
                className="mt-1 h-9 w-full rounded-md border border-input bg-transparent px-3"
              />
            </label>
            <label className="text-xs">
              Activity
              <select
                aria-label="Activity state"
                value={form.activity}
                onChange={(event) => setForm({ ...form, activity: event.target.value })}
                className="mt-1 h-9 w-full rounded-md border border-input bg-background px-2"
              >
                <option value="unknown">Unknown</option>
                <option value="resting">Resting</option>
                <option value="walking">Walking</option>
                <option value="other">Other</option>
              </select>
            </label>
            <label className="text-xs">
              Position
              <select
                aria-label="Position state"
                value={form.position}
                onChange={(event) => setForm({ ...form, position: event.target.value })}
                className="mt-1 h-9 w-full rounded-md border border-input bg-background px-2"
              >
                <option value="unknown">Unknown</option>
                <option value="sitting">Sitting</option>
                <option value="standing">Standing</option>
                <option value="lying">Lying</option>
                <option value="upright">Upright</option>
              </select>
            </label>
          </div>
          <div className="flex flex-wrap gap-2">
            <input
              aria-label="Activity interval notes"
              value={form.notes}
              onChange={(event) => setForm({ ...form, notes: event.target.value })}
              placeholder="Optional context"
              className="h-9 min-w-64 flex-1 rounded-md border border-input bg-transparent px-3 text-sm"
            />
            <Button size="sm" onClick={createInterval} disabled={saving} className="gap-1.5">
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
              Record interval
            </Button>
          </div>
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
        <div className="rounded-lg border p-3"><b>{data?.counts?.intervals || 0}</b><br />recorded intervals</div>
        <div className="rounded-lg border p-3"><b>{data?.counts?.manual_intervals || 0}</b><br />manual</div>
        <div className="rounded-lg border p-3"><b>{data?.counts?.wearable_intervals || 0}</b><br />wearable-inferred</div>
        <div className="rounded-lg border p-3"><b>{data?.counts?.overridden_intervals || 0}</b><br />retained overrides</div>
      </div>

      {effects.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {effects.map((effect) => (
            <article key={effect.id} className="rounded-lg border border-border bg-card p-3 text-xs">
              <div className="flex justify-between gap-2">
                <b>{effect.dimension}: {effect.state}</b>
                <span className="text-muted-foreground">
                  {effect.analytics_confidence.discovery_status.replaceAll("_", " ")}
                </span>
              </div>
              <p className="mt-1">
                {METRICS[effect.metric] || effect.metric}:{" "}
                <b>{effect.observed_mean} {effect.unit}</b>
              </p>
              <p className="text-muted-foreground mt-1">
                n={effect.sample_count}; {effect.measured_interval_count}/{effect.interval_count} intervals
                measured; replication {effect.replication_status.replaceAll("_", " ")}.
              </p>
              <p className="mt-1">{effect.language.lead} Not causal.</p>
            </article>
          ))}
        </div>
      )}

      {(data?.intervals || []).length === 0 ? (
        <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
          No timestamped activity or position intervals in this range. Daily step totals remain
          day-level context and are not treated as body-position evidence.
        </p>
      ) : (
        <div className="space-y-2">
          {(data?.intervals || []).slice().reverse().slice(0, 20).map((interval) => (
            <article key={interval.id} className={`rounded-lg border p-3 text-xs ${interval.effective ? "" : "opacity-60"}`}>
              <div className="flex flex-wrap justify-between gap-2">
                <div>
                  <b>{interval.activity} · {interval.position}</b>
                  <p>{formatTimestamp(interval.start_time)} → {formatTimestamp(interval.end_time)}</p>
                </div>
                <span className="uppercase text-[10px]">
                  {interval.origin_kind}
                  {interval.coverage_status === "partially_overridden"
                    ? " · partially overridden"
                    : interval.effective
                      ? ""
                      : " · overridden"}
                </span>
              </div>
              {interval.notes && <p className="mt-1">{interval.notes}</p>}
              {!readOnly && data?.can_edit && interval.effective && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => correctInterval(interval)}
                  className="mt-2"
                >
                  Append correction
                </Button>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
