import { useEffect, useState } from "react";
import { SlidersHorizontal } from "lucide-react";

// Glooko records a settings snapshot only occasionally, so the newest one can
// be months old. The card always leads with that snapshot's date and age and
// never labels the values as live.

function fmt(value) {
  if (value == null) return "—";
  return Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
}

function asOfDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function ageText(days) {
  if (days == null) return "";
  if (days <= 0) return "recorded today";
  return days === 1 ? "1 day old" : `${days} days old`;
}

function SegmentTable({ label, unit, segments }) {
  return (
    <div className="rounded-lg border border-border p-3">
      <div className="text-xs font-medium mb-1.5">
        {label} <span className="font-normal text-muted-foreground">· {unit}</span>
      </div>
      <table className="w-full text-xs tabular-nums">
        <tbody>
          {segments.map((s) => (
            <tr key={`${s.start}-${s.end}`} className="border-t border-border/50 first:border-t-0">
              <td className="py-0.5 text-muted-foreground">{s.start}–{s.end}</td>
              <td className="py-0.5 text-right font-medium">{fmt(s.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function PumpSettingsCard() {
  const [data, setData] = useState(null);

  useEffect(() => {
    let active = true;
    fetch("/api/pump-settings", { credentials: "same-origin" })
      .then((response) => (response.ok ? response.json() : null))
      .then((result) => { if (active) setData(result); })
      .catch(() => { if (active) setData(null); });
    return () => { active = false; };
  }, []);

  if (!data?.available || !data.latest) return null;
  const latest = data.latest;
  const stale = latest.age_days != null && latest.age_days > 30;
  const programs = [...(latest.basal_programs || [])].sort((a, b) => Number(b.active) - Number(a.active));

  return (
    <div className="bg-card rounded-xl border border-border p-5 space-y-4" data-testid="pump-settings-card">
      <div className="flex items-start gap-3">
        <SlidersHorizontal className="w-5 h-5 text-primary mt-0.5 flex-shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="font-semibold text-sm">Pump settings</h3>
            <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${stale ? "bg-amber-100 text-amber-800" : "bg-muted text-muted-foreground"}`}>
              as of {asOfDate(latest.snapshot_at)} · {ageText(latest.age_days)}
            </span>
          </div>
          <p className="text-xs text-muted-foreground mt-1">
            Programmed values from the most recent snapshot Glooko recorded{latest.device_model ? ` for the ${latest.device_model}` : ""}.
            Glooko records these only occasionally, so anything changed on the pump since then is not shown here.
          </p>
        </div>
      </div>

      {latest.scalars?.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {latest.scalars.map((s) => (
            <div key={s.key} className="rounded-lg border border-border p-3">
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{s.label}</div>
              <div className="text-base font-semibold tabular-nums">
                {fmt(s.value)}<span className="text-xs text-muted-foreground ml-0.5">{s.unit}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {(latest.schedules || []).map((schedule) => (
          <SegmentTable key={schedule.key} label={schedule.label} unit={schedule.unit} segments={schedule.segments} />
        ))}
      </div>

      {programs.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {programs.map((program) => (
            <SegmentTable
              key={program.name}
              label={`Basal: ${program.name}${program.active ? " (active)" : ""}`}
              unit={`U/h · ${fmt(program.units_per_day)} U/day`}
              segments={program.segments}
            />
          ))}
        </div>
      )}

      {data.changes?.length > 0 && (
        <div className="text-[11px] text-muted-foreground space-y-0.5">
          <div className="font-medium text-foreground">Changes between snapshots</div>
          {data.changes.slice(0, 5).map((change) => (
            <div key={change.snapshot_at}>
              {asOfDate(change.snapshot_at)}: {change.changed.join(", ") || "settings changed"}
            </div>
          ))}
        </div>
      )}
      <p className="text-[11px] text-muted-foreground">
        {data.snapshot_count} snapshot{data.snapshot_count === 1 ? "" : "s"} on file. Read-only; settings are changed on the pump with your care team.
      </p>
    </div>
  );
}
