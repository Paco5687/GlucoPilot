import { useCallback, useEffect, useState } from "react";
import { Check, Clock3, Loader2, Plus, X } from "lucide-react";
import { toast } from "sonner";

/** @param {import("react").InputHTMLAttributes<HTMLInputElement>} props */
function Input({ className = "", ...props }) {
  return <input className={`h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm ${className}`} {...props} />;
}

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

const EMPTY = {
  medication_name: "",
  dose: "",
  formulation: "",
  frequency: "",
  start_time: new Date().toISOString().slice(0, 10),
  end_time: "",
};

export default function MedicationExposures() {
  const [items, setItems] = useState([]);
  const [canEdit, setCanEdit] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState(EMPTY);

  const load = useCallback(async () => {
    try {
      const data = await api("/api/medication-exposures?include_dismissed=true");
      setItems(data.medication_exposures || []);
      setCanEdit(Boolean(data.can_edit));
    } catch (error) {
      toast.error(error.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function create() {
    if (!form.medication_name.trim() || saving) return;
    setSaving(true);
    try {
      const created = await api("/api/medication-exposures", {
        method: "POST",
        body: JSON.stringify({ ...form, end_time: form.end_time || null, origin_kind: "manual" }),
      });
      setItems((current) => [created, ...current]);
      setForm({ ...EMPTY, start_time: form.start_time });
      toast.success("Medication exposure recorded as proposed");
    } catch (error) {
      toast.error(error.message);
    } finally {
      setSaving(false);
    }
  }

  async function decide(item, status) {
    const reason = window.prompt(
      status === "confirmed" ? "Why are you confirming this exposure?" : "Why should it be dismissed?",
    );
    if (!reason?.trim()) return;
    try {
      const updated = await api(`/api/medication-exposures/${item.id}/decision`, {
        method: "POST",
        body: JSON.stringify({ status, reason }),
      });
      setItems((current) => current.map((value) => value.id === updated.id ? updated : value));
    } catch (error) {
      toast.error(error.message);
    }
  }

  async function correct(item) {
    const end = window.prompt(
      "Corrected end date (leave blank for ongoing)",
      item.end_time || "",
    );
    if (end === null) return;
    const reason = window.prompt("Why is this interval being corrected?");
    if (!reason?.trim()) return;
    try {
      const updated = await api(`/api/medication-exposures/${item.id}`, {
        method: "PUT",
        body: JSON.stringify({ end_time: end || null, reason }),
      });
      setItems((current) => current.map((value) => value.id === updated.id ? updated : value));
    } catch (error) {
      toast.error(error.message);
    }
  }

  if (loading) return null;

  return (
    <section className="rounded-xl border border-border bg-card p-5 space-y-3" aria-labelledby="medication-exposures-heading">
      <div className="flex gap-2">
        <Clock3 className="w-5 h-5 text-primary" />
        <div>
          <h3 id="medication-exposures-heading" className="font-semibold text-sm">Medication exposure intervals</h3>
          <p className="text-xs text-muted-foreground">
            Record when a dose and formulation were actually in use, including ongoing intervals.
          </p>
        </div>
      </div>

      {items.map((item) => (
        <div key={item.id} className="rounded-lg border border-border bg-muted/20 p-3 text-xs">
          <div className="flex flex-wrap justify-between gap-2">
            <p>
              <span className="font-semibold text-sm">{item.medication_name}</span>
              {item.dose ? ` · ${item.dose}` : ""}{item.formulation ? ` · ${item.formulation}` : ""}
              {item.frequency ? ` · ${item.frequency}` : ""}
            </p>
            <span className="uppercase font-semibold">{item.status}</span>
          </div>
          <p>{item.start_time} → {item.end_time || "ongoing"} · {item.origin_kind}</p>
          {canEdit && item.status === "proposed" && (
            <div className="flex flex-wrap gap-2 mt-2">
              <Button size="sm" variant="outline" onClick={() => correct(item)}>Correct interval</Button>
              <Button size="sm" onClick={() => decide(item, "confirmed")} className="gap-1">
                <Check className="h-3.5 w-3.5" /> Confirm
              </Button>
              <Button size="sm" variant="outline" onClick={() => decide(item, "dismissed")} className="gap-1">
                <X className="h-3.5 w-3.5" /> Dismiss
              </Button>
            </div>
          )}
        </div>
      ))}

      {canEdit && (
        <div className="grid grid-cols-1 sm:grid-cols-12 gap-2">
          <Input
            aria-label="Exposure medication name"
            value={form.medication_name}
            onChange={(event) => setForm({ ...form, medication_name: event.target.value })}
            placeholder="Medication"
            className="sm:col-span-3"
          />
          <Input
            aria-label="Exposure dose"
            value={form.dose}
            onChange={(event) => setForm({ ...form, dose: event.target.value })}
            placeholder="Dose"
            className="sm:col-span-2"
          />
          <Input
            aria-label="Exposure formulation"
            value={form.formulation}
            onChange={(event) => setForm({ ...form, formulation: event.target.value })}
            placeholder="Formulation"
            className="sm:col-span-2"
          />
          <Input
            aria-label="Exposure frequency"
            value={form.frequency}
            onChange={(event) => setForm({ ...form, frequency: event.target.value })}
            placeholder="Frequency"
            className="sm:col-span-2"
          />
          <Input
            aria-label="Exposure start"
            type="date"
            value={form.start_time}
            onChange={(event) => setForm({ ...form, start_time: event.target.value })}
            className="sm:col-span-3"
          />
          <Input
            aria-label="Exposure end"
            type="date"
            value={form.end_time}
            onChange={(event) => setForm({ ...form, end_time: event.target.value })}
            className="sm:col-span-3"
          />
          <div className="sm:col-span-9 flex items-center justify-between gap-2">
            <span className="text-[11px] text-muted-foreground">
              Leave the end blank for an ongoing exposure. New intervals require confirmation.
            </span>
            <Button size="sm" onClick={create} disabled={saving || !form.medication_name.trim()} className="gap-1">
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
              Add interval
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
