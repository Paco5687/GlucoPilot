import { useState, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NotebookPen, Plus, X, Loader2, Moon } from "lucide-react";
import { toast } from "sonner";

const SEV = [
  { n: 1, label: "very mild", tone: "bg-emerald-100 text-emerald-700 border-emerald-200" },
  { n: 2, label: "mild", tone: "bg-lime-100 text-lime-700 border-lime-200" },
  { n: 3, label: "moderate", tone: "bg-amber-100 text-amber-700 border-amber-200" },
  { n: 4, label: "severe", tone: "bg-orange-100 text-orange-700 border-orange-200" },
  { n: 5, label: "very severe", tone: "bg-rose-100 text-rose-700 border-rose-200" },
];
const SEV_BY_N = Object.fromEntries(SEV.map((s) => [s.n, s]));
const TIMES = ["morning", "afternoon", "evening", "night", "all day"];

const EMPTY = { title: "", description: "", severity: 3, duration: "", time_of_day: "" };

function prettyDate(iso, today) {
  if (iso === today) return "Today";
  const d = new Date(iso + "T00:00:00");
  const y = new Date(today + "T00:00:00"); y.setDate(y.getDate() - 1);
  if (iso === y.toISOString().slice(0, 10)) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

export default function Symptoms() {
  const [items, setItems] = useState([]);
  const [today, setToday] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [f, setF] = useState(EMPTY);

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/symptoms?days=120", { credentials: "same-origin" });
      if (r.ok) { const d = await r.json(); setItems(d.symptoms || []); setToday(d.today || ""); }
    } catch { /* */ }
    setLoading(false);
  }, []);
  useEffect(() => { load(); }, [load]);

  async function add() {
    if (!f.title.trim() || saving) return;
    setSaving(true);
    try {
      const r = await fetch("/api/symptoms", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify(f),
      });
      if (!r.ok) throw new Error("Save failed");
      const d = await r.json();
      setItems(d.symptoms || []); setToday(d.today || today);
      setF({ ...EMPTY, time_of_day: f.time_of_day }); // keep time-of-day for quick repeated logging
      toast.success("Logged");
    } catch (err) { toast.error(err.message); }
    setSaving(false);
  }
  async function remove(id) {
    const r = await fetch(`/api/symptoms/${id}`, { method: "DELETE", credentials: "same-origin" });
    if (r.ok) setItems((await r.json()).symptoms || []);
  }

  // group by entry_date (already newest-first from the API)
  const byDate = [];
  for (const it of items) {
    const g = byDate.find((x) => x.date === it.entry_date);
    if (g) g.rows.push(it); else byDate.push({ date: it.entry_date, rows: [it] });
  }

  // recurring rollup
  const agg = {};
  for (const it of items) {
    const k = (it.title || "").trim().toLowerCase();
    if (!k) continue;
    (agg[k] = agg[k] || { title: it.title, sev: [] }).sev.push(it.severity || 3);
  }
  const recurring = Object.values(agg)
    .map((a) => ({ title: a.title, count: a.sev.length, avg: a.sev.reduce((x, y) => x + y, 0) / a.sev.length }))
    .filter((a) => a.count > 1)
    .sort((a, b) => b.count - a.count).slice(0, 10);

  return (
    <div className="space-y-4 max-w-4xl">
      <div>
        <h1 className="text-xl font-bold flex items-center gap-2"><NotebookPen className="w-5 h-5 text-primary" /> Symptom journal</h1>
        <p className="text-sm text-muted-foreground mt-1 flex items-center gap-1.5">
          <Moon className="w-3.5 h-3.5" /> A nightly check-in. Log how you felt today — it becomes part of your health history and the Companion, Overview, and Visit Report can all see it.
        </p>
      </div>

      {/* Add form */}
      <div className="bg-card rounded-xl border border-border p-5 space-y-3">
        <div className="grid grid-cols-1 sm:grid-cols-12 gap-2 items-end">
          <div className="sm:col-span-5">
            <label className="text-[11px] text-muted-foreground">Symptom</label>
            <Input value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} onKeyDown={(e) => e.key === "Enter" && add()} placeholder="e.g. Joint pain, Fatigue, Headache" className="mt-1 text-sm" />
          </div>
          <div className="sm:col-span-4">
            <label className="text-[11px] text-muted-foreground">Duration</label>
            <Input value={f.duration} onChange={(e) => setF({ ...f, duration: e.target.value })} placeholder="e.g. all day, ~2 hrs, came & went" className="mt-1 text-sm" />
          </div>
          <div className="sm:col-span-3">
            <label className="text-[11px] text-muted-foreground">Time of day</label>
            <select value={f.time_of_day} onChange={(e) => setF({ ...f, time_of_day: e.target.value })} className="mt-1 w-full h-9 rounded-md border border-border bg-background px-2 text-sm">
              <option value="">—</option>
              {TIMES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
        </div>
        <div>
          <label className="text-[11px] text-muted-foreground">Severity</label>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {SEV.map((s) => (
              <button key={s.n} type="button" onClick={() => setF({ ...f, severity: s.n })}
                className={`px-2.5 py-1 rounded-full text-xs font-medium border transition-colors ${f.severity === s.n ? s.tone : "bg-muted/40 text-muted-foreground border-transparent hover:bg-muted"}`}>
                {s.n} · {s.label}
              </button>
            ))}
          </div>
        </div>
        <Input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} onKeyDown={(e) => e.key === "Enter" && add()} placeholder="Notes — what it felt like, what you think triggered it (optional)" className="text-sm" />
        <div className="flex justify-end">
          <Button size="sm" onClick={add} disabled={saving || !f.title.trim()} className="gap-1.5">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} Log symptom
          </Button>
        </div>
      </div>

      {recurring.length > 0 && (
        <div className="bg-card rounded-xl border border-border p-4">
          <p className="text-[10px] uppercase tracking-wide text-muted-foreground mb-2">Recurring (last 120 days)</p>
          <div className="flex flex-wrap gap-1.5">
            {recurring.map((r) => (
              <span key={r.title} className={`text-xs px-2 py-1 rounded-full border ${SEV_BY_N[Math.round(r.avg)]?.tone || ""}`}>
                {r.title} <span className="opacity-70">×{r.count}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {/* History */}
      {loading ? (
        <div className="flex justify-center py-10"><Loader2 className="w-5 h-5 animate-spin text-muted-foreground" /></div>
      ) : byDate.length === 0 ? (
        <div className="bg-card rounded-xl border border-border p-8 text-center text-sm text-muted-foreground">
          No symptoms logged yet. Tonight, jot down anything that stood out about how you felt today.
        </div>
      ) : (
        <div className="space-y-4">
          {byDate.map((day) => (
            <div key={day.date}>
              <div className="text-xs font-semibold text-muted-foreground mb-1.5">{prettyDate(day.date, today)}</div>
              <div className="space-y-1.5">
                {day.rows.map((it) => {
                  const s = SEV_BY_N[it.severity] || SEV_BY_N[3];
                  return (
                    <div key={it.id} className="group flex items-start gap-3 bg-card rounded-lg border border-border px-3 py-2 text-sm">
                      <span className={`mt-0.5 text-[10px] px-1.5 py-0.5 rounded-full border font-medium whitespace-nowrap ${s.tone}`}>{s.n} · {s.label}</span>
                      <div className="flex-1 min-w-0">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                          <span className="font-medium">{it.title}</span>
                          {it.time_of_day && <span className="text-xs text-muted-foreground">· {it.time_of_day}</span>}
                          {it.duration && <span className="text-xs text-muted-foreground">· {it.duration}</span>}
                        </div>
                        {it.description && <p className="text-xs text-muted-foreground mt-0.5 leading-snug">{it.description}</p>}
                      </div>
                      <button onClick={() => remove(it.id)} className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive flex-shrink-0"><X className="w-3.5 h-3.5" /></button>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
