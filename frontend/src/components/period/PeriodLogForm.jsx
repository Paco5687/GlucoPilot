import { useState, useEffect } from "react";
import { format } from "date-fns";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Save, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";

const SYMPTOMS = [
  "cramps", "bloating", "headache", "fatigue",
  "mood_swings", "breast_tenderness", "backache", "nausea",
];

const SYMPTOM_LABELS = {
  cramps: "Cramps", bloating: "Bloating", headache: "Headache",
  fatigue: "Fatigue", mood_swings: "Mood Swings",
  breast_tenderness: "Breast Tenderness", backache: "Backache", nausea: "Nausea",
};

export default function PeriodLogForm({ date, existingLog, onSave, onDelete }) {
  const [phase, setPhase] = useState("");
  const [flow, setFlow] = useState("");
  const [symptoms, setSymptoms] = useState([]);
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (existingLog) {
      setPhase(existingLog.phase || "");
      setFlow(existingLog.flow || "");
      setSymptoms(existingLog.symptoms ? existingLog.symptoms.split(",").filter(Boolean) : []);
      setNotes(existingLog.notes || "");
    } else {
      setPhase("");
      setFlow("");
      setSymptoms([]);
      setNotes("");
    }
  }, [existingLog, date]);

  const toggleSymptom = (s) => {
    setSymptoms((prev) => prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]);
  };

  const handleSave = async () => {
    setSaving(true);
    await onSave({
      date: format(date, "yyyy-MM-dd"),
      phase: phase || undefined,
      flow: flow || undefined,
      symptoms: symptoms.length ? symptoms.join(",") : undefined,
      notes: notes || undefined,
      source: "manual",
    });
    setSaving(false);
  };

  if (!date) {
    return (
      <div className="bg-card rounded-xl border border-border p-6 text-center text-muted-foreground">
        Select a date on the calendar to log period data.
      </div>
    );
  }

  return (
    <div className="bg-card rounded-xl border border-border p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">{format(date, "EEEE, MMM d, yyyy")}</h3>
        {existingLog && (
          <Button variant="ghost" size="sm" className="text-destructive" onClick={() => onDelete(existingLog)}>
            <Trash2 className="w-3.5 h-3.5 mr-1" /> Delete
          </Button>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs font-medium text-muted-foreground mb-1 block">Phase</label>
          <Select value={phase} onValueChange={setPhase}>
            <SelectTrigger><SelectValue placeholder="Select phase" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="menstrual">Menstrual</SelectItem>
              <SelectItem value="follicular">Follicular</SelectItem>
              <SelectItem value="ovulation">Ovulation</SelectItem>
              <SelectItem value="luteal">Luteal</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div>
          <label className="text-xs font-medium text-muted-foreground mb-1 block">Flow</label>
          <Select value={flow} onValueChange={setFlow}>
            <SelectTrigger><SelectValue placeholder="Select flow" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="none">None</SelectItem>
              <SelectItem value="spotting">Spotting</SelectItem>
              <SelectItem value="light">Light</SelectItem>
              <SelectItem value="medium">Medium</SelectItem>
              <SelectItem value="heavy">Heavy</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      <div>
        <label className="text-xs font-medium text-muted-foreground mb-2 block">Symptoms</label>
        <div className="flex flex-wrap gap-2">
          {SYMPTOMS.map((s) => (
            <Badge
              key={s}
              variant={symptoms.includes(s) ? "default" : "outline"}
              className={cn("cursor-pointer transition-all", symptoms.includes(s) && "bg-primary")}
              onClick={() => toggleSymptom(s)}
            >
              {SYMPTOM_LABELS[s]}
            </Badge>
          ))}
        </div>
      </div>

      <div>
        <label className="text-xs font-medium text-muted-foreground mb-1 block">Notes</label>
        <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Optional notes…" className="h-16" />
      </div>

      <Button onClick={handleSave} disabled={saving} className="w-full">
        <Save className="w-4 h-4 mr-2" />
        {saving ? "Saving…" : existingLog ? "Update Log" : "Save Log"}
      </Button>
    </div>
  );
}