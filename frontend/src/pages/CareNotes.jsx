import { useState, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Loader2, ClipboardList, Pin, PencilLine, Trash2, Plus, X } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/lib/AuthContext";

// The one place providers write directly into the app: routines, protocols,
// prescription instructions. Everyone signed in can read every note; each note
// is stamped with its author and only the author can edit it (the owner can
// delete anything).

const KIND_TONE = {
  routine: "bg-emerald-500/10 text-emerald-600",
  protocol: "bg-blue-500/10 text-blue-600",
  prescription: "bg-violet-500/10 text-violet-600",
  instruction: "bg-amber-500/10 text-amber-700",
  note: "bg-muted text-muted-foreground",
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.detail || `Request failed (${res.status})`);
  return data;
}

const EMPTY_FORM = { kind: "note", title: "", body: "", pinned: false };

function NoteForm({ initial, kinds, onCancel, onSaved, submitLabel }) {
  const [form, setForm] = useState(initial);
  const [saving, setSaving] = useState(false);

  async function save() {
    if (!form.title.trim() && !form.body.trim()) { toast.error("Write something first"); return; }
    setSaving(true);
    try {
      await onSaved(form);
    } catch (err) {
      toast.error(err.message || "Could not save the note");
    }
    setSaving(false);
  }

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label="Note kind"
          value={form.kind}
          onChange={(e) => setForm({ ...form, kind: e.target.value })}
          className="h-8 rounded-md border border-border bg-background px-2 text-xs capitalize"
        >
          {kinds.map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
        <Input
          aria-label="Note title"
          placeholder="Title (e.g. Overnight basal protocol)"
          value={form.title}
          onChange={(e) => setForm({ ...form, title: e.target.value })}
          className="h-8 text-sm flex-1 min-w-48"
        />
        <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <input type="checkbox" checked={form.pinned} onChange={(e) => setForm({ ...form, pinned: e.target.checked })} />
          <Pin className="w-3 h-3" /> Pin
        </label>
      </div>
      <textarea
        aria-label="Note body"
        placeholder="Routines, protocols, prescription instructions…"
        value={form.body}
        onChange={(e) => setForm({ ...form, body: e.target.value })}
        rows={5}
        className="w-full rounded-md border border-border bg-background p-2 text-sm"
      />
      <div className="flex gap-2 justify-end">
        <Button variant="ghost" size="sm" onClick={onCancel} className="gap-1 text-xs"><X className="w-3.5 h-3.5" /> Cancel</Button>
        <Button size="sm" onClick={save} disabled={saving} className="gap-1 text-xs">
          {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />} {submitLabel}
        </Button>
      </div>
    </div>
  );
}

export default function CareNotes() {
  const { isProvider } = useAuth();
  const [notes, setNotes] = useState([]);
  const [me, setMe] = useState("");
  const [kinds, setKinds] = useState(["routine", "protocol", "prescription", "instruction", "note"]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [editingId, setEditingId] = useState(null);

  const load = useCallback(async () => {
    try {
      const data = await api("/api/care-notes");
      setNotes(data.notes || []);
      setMe(data.me || "");
      if (data.kinds?.length) setKinds(data.kinds);
    } catch { /* auth gate handles redirects */ }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  async function createNote(form) {
    await api("/api/care-notes", { method: "POST", body: JSON.stringify(form) });
    setCreating(false);
    toast.success("Note added");
    await load();
  }

  async function updateNote(id, form) {
    await api(`/api/care-notes/${id}`, { method: "PUT", body: JSON.stringify(form) });
    setEditingId(null);
    toast.success("Note updated");
    await load();
  }

  async function deleteNote(note) {
    if (!window.confirm(`Delete "${note.title || "this note"}"?`)) return;
    try {
      await api(`/api/care-notes/${note.id}`, { method: "DELETE" });
      toast.success("Note deleted");
      await load();
    } catch (err) {
      toast.error(err.message || "Could not delete the note");
    }
  }

  const canDelete = (note) => note.author === me || !isProvider;

  return (
    <div className="space-y-4 max-w-3xl">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-lg font-semibold flex items-center gap-2">
            <ClipboardList className="w-5 h-5 text-primary" /> Care Notes
          </h1>
          <p className="text-xs text-muted-foreground">
            Shared between you and the care team — routines, protocols, and prescription
            instructions, each signed by whoever wrote it.
          </p>
        </div>
        {!creating && (
          <Button size="sm" onClick={() => setCreating(true)} className="gap-1.5 text-xs">
            <Plus className="w-3.5 h-3.5" /> New note
          </Button>
        )}
      </div>

      {creating && (
        <NoteForm
          initial={EMPTY_FORM}
          kinds={kinds}
          submitLabel="Add note"
          onCancel={() => setCreating(false)}
          onSaved={createNote}
        />
      )}

      {loading ? (
        <div className="flex items-center justify-center h-32"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>
      ) : notes.length === 0 && !creating ? (
        <div className="bg-card rounded-xl border border-border p-8 text-center text-sm text-muted-foreground">
          No notes yet. Anything the care team should follow — or anything you want them to see — starts here.
        </div>
      ) : (
        <div className="space-y-3">
          {notes.map((note) => (
            editingId === note.id ? (
              <NoteForm
                key={note.id}
                initial={{ kind: note.kind || "note", title: note.title || "", body: note.body || "", pinned: !!note.pinned }}
                kinds={kinds}
                submitLabel="Save changes"
                onCancel={() => setEditingId(null)}
                onSaved={(form) => updateNote(note.id, form)}
              />
            ) : (
              <div key={note.id} className="bg-card rounded-xl border border-border p-4 space-y-2">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-center gap-2 min-w-0">
                    {note.pinned && <Pin className="w-3.5 h-3.5 text-primary shrink-0" />}
                    <span className={`text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 shrink-0 ${KIND_TONE[note.kind] || KIND_TONE.note}`}>
                      {note.kind}
                    </span>
                    <h2 className="text-sm font-semibold truncate">{note.title || "Untitled"}</h2>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    {note.author === me && (
                      <Button variant="ghost" size="sm" onClick={() => setEditingId(note.id)} className="h-7 px-2 text-xs gap-1">
                        <PencilLine className="w-3 h-3" /> Edit
                      </Button>
                    )}
                    {canDelete(note) && (
                      <Button variant="ghost" size="sm" onClick={() => deleteNote(note)} className="h-7 px-2 text-xs text-destructive gap-1">
                        <Trash2 className="w-3 h-3" />
                      </Button>
                    )}
                  </div>
                </div>
                {note.body && <p className="text-sm whitespace-pre-wrap">{note.body}</p>}
                <p className="text-[11px] text-muted-foreground">
                  {note.author_name}{note.updated_date ? ` · updated ${String(note.updated_date).slice(0, 10)}` : ""}
                </p>
              </div>
            )
          ))}
        </div>
      )}
    </div>
  );
}
