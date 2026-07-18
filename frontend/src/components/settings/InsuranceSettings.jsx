import { useState, useEffect, useRef } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ShieldCheck, ScanLine, Loader2, Save } from "lucide-react";
import { toast } from "sonner";

const FIELDS = [
  { key: "carrier", label: "Carrier" },
  { key: "plan_name", label: "Plan name" },
  { key: "plan_type", label: "Plan type (PPO/HMO)" },
  { key: "member_name", label: "Member name" },
  { key: "member_id", label: "Member ID" },
  { key: "group_number", label: "Group number" },
  { key: "rx_bin", label: "RxBIN" },
  { key: "rx_pcn", label: "RxPCN" },
  { key: "rx_group", label: "RxGroup" },
  { key: "customer_service_phone", label: "Member services phone" },
  { key: "effective_date", label: "Effective date" },
];

const EMPTY = Object.fromEntries([...FIELDS.map((f) => f.key), "notes"].map((k) => [k, ""]));

export default function InsuranceSettings() {
  const [form, setForm] = useState(EMPTY);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [scanning, setScanning] = useState(false);
  const frontRef = useRef(null);

  useEffect(() => {
    fetch("/api/insurance", { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : {}))
      .then((d) => setForm({ ...EMPTY, ...d }))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const set = (k, v) => setForm((p) => ({ ...p, [k]: v }));

  async function handleScan(files) {
    if (!files?.length) return;
    setScanning(true);
    toast.info("Reading the card with the on-server model…");
    try {
      const fd = new FormData();
      fd.append("file", files[0]);
      if (files[1]) fd.append("back", files[1]);
      const res = await fetch("/api/insurance/extract", { method: "POST", body: fd, credentials: "same-origin" });
      const data = await res.json().catch(() => null);
      if (!res.ok) throw new Error(data?.detail || `Scan failed (${res.status})`);
      // Merge only non-empty extracted values so we don't wipe typed fields.
      setForm((p) => {
        const next = { ...p };
        for (const [k, v] of Object.entries(data)) if (v) next[k] = v;
        return next;
      });
      toast.success("Card scanned — review the fields and Save.");
    } catch (err) {
      toast.error(err.message || "Scan failed");
    }
    setScanning(false);
    if (frontRef.current) frontRef.current.value = "";
  }

  async function handleSave() {
    setSaving(true);
    try {
      const res = await fetch("/api/insurance", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(form),
      });
      if (!res.ok) throw new Error("Save failed");
      toast.success("Insurance saved — it will print on the Visit Report.");
    } catch (err) {
      toast.error(err.message || "Save failed");
    }
    setSaving(false);
  }

  if (loading) return null;

  return (
    <div className="bg-card rounded-xl border border-border p-5 space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <ShieldCheck className="w-5 h-5 text-primary" />
          <div>
            <h3 className="font-semibold text-sm">Health Insurance</h3>
            <p className="text-xs text-muted-foreground">Prints on the Visit Report so it's ready at appointments.</p>
          </div>
        </div>
        <div>
          <input
            ref={frontRef}
            type="file"
            accept=".png,.jpg,.jpeg,.webp"
            multiple
            className="hidden"
            onChange={(e) => handleScan(e.target.files)}
          />
          <Button variant="outline" size="sm" onClick={() => frontRef.current?.click()} disabled={scanning} className="gap-1.5 text-xs">
            {scanning ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ScanLine className="w-3.5 h-3.5" />}
            {scanning ? "Reading…" : "Scan card"}
          </Button>
        </div>
      </div>

      <p className="text-[11px] text-muted-foreground -mt-2">
        Tip: select the card photo(s) — front, or front + back — to auto-fill. Extraction runs on this server.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {FIELDS.map((f) => (
          <div key={f.key}>
            <Label htmlFor={`ins_${f.key}`} className="text-xs">{f.label}</Label>
            <Input
              id={`ins_${f.key}`}
              className="mt-1 text-sm"
              value={form[f.key] || ""}
              onChange={(e) => set(f.key, e.target.value)}
            />
          </div>
        ))}
      </div>
      <div>
        <Label htmlFor="ins_notes" className="text-xs">Notes</Label>
        <Input id="ins_notes" className="mt-1 text-sm" value={form.notes || ""} onChange={(e) => set("notes", e.target.value)} placeholder="e.g. secondary coverage, referrals required" />
      </div>

      <Button onClick={handleSave} disabled={saving} size="sm" className="gap-2">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
        Save insurance
      </Button>
    </div>
  );
}
