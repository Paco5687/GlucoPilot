import { useState, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollText, Plus, X, Loader2, Save, BookOpen } from "lucide-react";
import { toast } from "sonner";

const KINDS = [
  { key: "diagnosis", label: "Diagnosis", tone: "bg-violet-100 text-violet-700 border-violet-200" },
  { key: "exposure", label: "Exposure", tone: "bg-amber-100 text-amber-700 border-amber-200" },
  { key: "injury", label: "Injury", tone: "bg-rose-100 text-rose-700 border-rose-200" },
  { key: "prescription", label: "Prescription", tone: "bg-blue-100 text-blue-700 border-blue-200" },
  { key: "hospital", label: "ER / Hospital", tone: "bg-red-100 text-red-700 border-red-200" },
  { key: "appointment", label: "Appointment", tone: "bg-teal-100 text-teal-700 border-teal-200" },
  { key: "advice", label: "Doctor advice", tone: "bg-emerald-100 text-emerald-700 border-emerald-200" },
  { key: "note", label: "Note", tone: "bg-muted text-muted-foreground border-transparent" },
];
const KIND_BY = Object.fromEntries(KINDS.map((k) => [k.key, k]));
const EMPTY = { title: "", kind: "diagnosis", entry_date: "", details: "" };

function prettyDate(iso) {
  if (!iso) return "undated";
  const d = new Date(iso + "T00:00:00");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export default function History() {
  const [narrative, setNarrative] = useState("");
  const [savedNarrative, setSavedNarrative] = useState("");
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [savingN, setSavingN] = useState(false);
  const [savingE, setSavingE] = useState(false);
  const [f, setF] = useState(EMPTY);

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/history", { credentials: "same-origin" });
      if (r.ok) {
        const d = await r.json();
        setNarrative(d.narrative || ""); setSavedNarrative(d.narrative || "");
        setEntries(d.entries || []);
      }
    } catch { /* */ }
    setLoading(false);
  }, []);
  useEffect(() => { load(); }, [load]);

  async function saveNarrative() {
    setSavingN(true);
    try {
      const r = await fetch("/api/history/narrative", {
        method: "PUT", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify({ narrative }),
      });
      if (!r.ok) throw new Error("Save failed");
      setSavedNarrative((await r.json()).narrative || "");
      toast.success("History narrative saved");
    } catch (err) { toast.error(err.message); }
    setSavingN(false);
  }

  async function addEntry() {
    if (!f.title.trim() || savingE) return;
    setSavingE(true);
    try {
      const r = await fetch("/api/history", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify(f),
      });
      if (!r.ok) throw new Error("Save failed");
      setEntries((await r.json()).entries || []);
      setF({ ...EMPTY, kind: f.kind });
      toast.success("Added to timeline");
    } catch (err) { toast.error(err.message); }
    setSavingE(false);
  }
  async function remove(id) {
    const r = await fetch(`/api/history/${id}`, { method: "DELETE", credentials: "same-origin" });
    if (r.ok) setEntries((await r.json()).entries || []);
  }

  const dirty = narrative !== savedNarrative;

  return (
    <div className="space-y-4 max-w-4xl">
      <div>
        <h1 className="text-xl font-bold flex items-center gap-2"><ScrollText className="w-5 h-5 text-primary" /> Health history</h1>
        <p className="text-sm text-muted-foreground mt-1">
          The story behind the numbers — a background narrative plus a timeline of medical events. Everything here becomes context the Companion and your Visit Report reason over.
        </p>
      </div>

      {/* Narrative */}
      <div className="bg-card rounded-xl border border-border p-5 space-y-3">
        <div className="flex items-center gap-2">
          <BookOpen className="w-4 h-4 text-primary" />
          <h3 className="font-semibold text-sm">Background narrative</h3>
        </div>
        <p className="text-xs text-muted-foreground -mt-1">Write the longer story of what's been going on — onset, major events, how things have unfolded. Edit it anytime.</p>
        <textarea
          value={narrative}
          onChange={(e) => setNarrative(e.target.value)}
          placeholder="e.g. Diagnosed with Type 1 in 2015 and Hashimoto's in 2018. Moved into a water-damaged apartment in early 2023; brain fog and fatigue began that spring. …"
          rows={8}
          className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm leading-relaxed resize-y focus:outline-none focus:ring-2 focus:ring-primary/30"
        />
        <div className="flex justify-end">
          <Button size="sm" onClick={saveNarrative} disabled={savingN || !dirty} className="gap-1.5">
            {savingN ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            {dirty ? "Save narrative" : "Saved"}
          </Button>
        </div>
      </div>

      {/* Add timeline event */}
      <div className="bg-card rounded-xl border border-border p-5 space-y-3">
        <h3 className="font-semibold text-sm">Add a timeline event</h3>
        <div className="grid grid-cols-1 sm:grid-cols-12 gap-2 items-end">
          <div className="sm:col-span-3">
            <label className="text-[11px] text-muted-foreground">Type</label>
            <select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })} className="mt-1 w-full h-9 rounded-md border border-border bg-background px-2 text-sm">
              {KINDS.map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}
            </select>
          </div>
          <div className="sm:col-span-3">
            <label className="text-[11px] text-muted-foreground">Date</label>
            <Input type="date" value={f.entry_date} onChange={(e) => setF({ ...f, entry_date: e.target.value })} className="mt-1 text-sm" />
          </div>
          <div className="sm:col-span-6">
            <label className="text-[11px] text-muted-foreground">What happened</label>
            <Input value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} onKeyDown={(e) => e.key === "Enter" && addEntry()} placeholder="e.g. Hashimoto's diagnosis" className="mt-1 text-sm" />
          </div>
        </div>
        <Input value={f.details} onChange={(e) => setF({ ...f, details: e.target.value })} onKeyDown={(e) => e.key === "Enter" && addEntry()} placeholder="Details (optional) — provider, meds started, advice given…" className="text-sm" />
        <div className="flex justify-end">
          <Button size="sm" onClick={addEntry} disabled={savingE || !f.title.trim()} className="gap-1.5">
            {savingE ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} Add event
          </Button>
        </div>
      </div>

      {/* Timeline */}
      {loading ? (
        <div className="flex justify-center py-8"><Loader2 className="w-5 h-5 animate-spin text-muted-foreground" /></div>
      ) : entries.length === 0 ? (
        <p className="text-sm text-muted-foreground text-center py-6">No timeline events yet. Add diagnoses, exposures, injuries, hospital visits, or advice as they happen.</p>
      ) : (
        <div className="space-y-1.5">
          {entries.map((e) => {
            const k = KIND_BY[e.kind] || KIND_BY.note;
            return (
              <div key={e.id} className="group flex items-start gap-3 bg-card rounded-lg border border-border px-3 py-2.5 text-sm">
                <span className="text-xs text-muted-foreground tabular-nums w-24 flex-shrink-0 pt-0.5">{prettyDate(e.entry_date)}</span>
                <span className={`text-[10px] px-1.5 py-0.5 rounded-full border font-medium whitespace-nowrap ${k.tone}`}>{k.label}</span>
                <div className="flex-1 min-w-0">
                  <span className="font-medium">{e.title}</span>
                  {e.details && <p className="text-xs text-muted-foreground mt-0.5 leading-snug">{e.details}</p>}
                </div>
                <button onClick={() => remove(e.id)} className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive flex-shrink-0"><X className="w-3.5 h-3.5" /></button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
