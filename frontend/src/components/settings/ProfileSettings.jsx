import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { User, Save, Loader2 } from "lucide-react";
import { toast } from "sonner";

const CM_PER_IN = 2.54;
const KG_PER_LB = 0.453592;

export default function ProfileSettings() {
  const [units, setUnits] = useState("imperial");
  const [ft, setFt] = useState("");
  const [inch, setInch] = useState("");
  const [cm, setCm] = useState("");
  const [weight, setWeight] = useState(""); // lbs or kg per units
  const [dob, setDob] = useState("");
  const [sex, setSex] = useState("");
  const [derived, setDerived] = useState({ age: null, bmi: null });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    fetch("/api/profile", { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : {}))
      .then((p) => {
        const u = p.units || "imperial";
        setUnits(u);
        setDob(p.date_of_birth || "");
        setSex(p.sex || "");
        setDerived({ age: p.age, bmi: p.bmi });
        if (p.height_cm) {
          if (u === "metric") setCm(String(Math.round(p.height_cm)));
          else {
            const totalIn = p.height_cm / CM_PER_IN;
            setFt(String(Math.floor(totalIn / 12)));
            setInch(String(Math.round(totalIn % 12)));
          }
        }
        if (p.weight_kg) setWeight(String(Math.round((u === "metric" ? p.weight_kg : p.weight_kg / KG_PER_LB) * 10) / 10));
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  function toMetric() {
    let height_cm = null;
    if (units === "metric") height_cm = cm ? parseFloat(cm) : null;
    else if (ft || inch) height_cm = (parseFloat(ft || 0) * 12 + parseFloat(inch || 0)) * CM_PER_IN;
    let weight_kg = null;
    if (weight) weight_kg = units === "metric" ? parseFloat(weight) : parseFloat(weight) * KG_PER_LB;
    return { height_cm: height_cm ? Math.round(height_cm * 10) / 10 : null, weight_kg: weight_kg ? Math.round(weight_kg * 10) / 10 : null };
  }

  async function handleSave() {
    setSaving(true);
    try {
      const { height_cm, weight_kg } = toMetric();
      const res = await fetch("/api/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ height_cm, weight_kg, date_of_birth: dob, sex, units }),
      });
      if (!res.ok) throw new Error("Save failed");
      const p = await res.json();
      setDerived({ age: p.age, bmi: p.bmi });
      toast.success("Profile saved.");
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
          <User className="w-5 h-5 text-primary" />
          <div>
            <h3 className="font-semibold text-sm">Body profile</h3>
            <p className="text-xs text-muted-foreground">Feeds BMI, TDD/kg, and the insulin resistance &amp; absorption estimates.</p>
          </div>
        </div>
        <div className="flex rounded-lg border border-border overflow-hidden text-xs">
          {["imperial", "metric"].map((u) => (
            <button key={u} onClick={() => setUnits(u)}
              className={`px-2.5 py-1 capitalize ${units === u ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}>{u}</button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <Label className="text-xs">Height</Label>
          {units === "metric" ? (
            <div className="flex items-center gap-1 mt-1"><Input value={cm} onChange={(e) => setCm(e.target.value)} className="text-sm" inputMode="numeric" /><span className="text-xs text-muted-foreground">cm</span></div>
          ) : (
            <div className="flex items-center gap-1 mt-1">
              <Input value={ft} onChange={(e) => setFt(e.target.value)} className="text-sm" inputMode="numeric" /><span className="text-xs text-muted-foreground">ft</span>
              <Input value={inch} onChange={(e) => setInch(e.target.value)} className="text-sm" inputMode="numeric" /><span className="text-xs text-muted-foreground">in</span>
            </div>
          )}
        </div>
        <div>
          <Label className="text-xs">Weight</Label>
          <div className="flex items-center gap-1 mt-1"><Input value={weight} onChange={(e) => setWeight(e.target.value)} className="text-sm" inputMode="decimal" /><span className="text-xs text-muted-foreground">{units === "metric" ? "kg" : "lb"}</span></div>
        </div>
        <div>
          <Label htmlFor="dob" className="text-xs">Date of birth</Label>
          <Input id="dob" type="date" value={dob} onChange={(e) => setDob(e.target.value)} className="mt-1 text-sm" />
        </div>
        <div>
          <Label htmlFor="sex" className="text-xs">Sex</Label>
          <select id="sex" value={sex} onChange={(e) => setSex(e.target.value)} className="mt-1 w-full h-9 rounded-md border border-border bg-background px-2 text-sm">
            <option value="">—</option>
            <option value="female">Female</option>
            <option value="male">Male</option>
            <option value="other">Other</option>
          </select>
        </div>
      </div>

      {(derived.age != null || derived.bmi != null) && (
        <p className="text-xs text-muted-foreground">
          {derived.age != null && <>Age <b>{derived.age}</b></>}
          {derived.bmi != null && <> · BMI <b>{derived.bmi}</b></>}
        </p>
      )}

      <Button onClick={handleSave} disabled={saving} size="sm" className="gap-2">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} Save profile
      </Button>
    </div>
  );
}
