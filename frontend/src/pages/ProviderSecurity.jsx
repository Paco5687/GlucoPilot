import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Loader2, KeyRound } from "lucide-react";
import { toast } from "sonner";

// A logged-in provider manages their own recovery questions. Re-keying requires
// the current password, so an unattended session can't have its recovery path
// swapped by a passerby. Accounts created before questions existed set them
// here for the first time.

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

const EMPTY = [
  { question: "", answer: "" },
  { question: "", answer: "" },
  { question: "", answer: "" },
];

export default function ProviderSecurity() {
  const [existing, setExisting] = useState(null);
  const [questions, setQuestions] = useState(EMPTY);
  const [currentPassword, setCurrentPassword] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api("/api/provider/security-questions")
      .then((r) => setExisting(r.questions || []))
      .catch(() => setExisting([]));
  }, []);

  async function save() {
    setBusy(true);
    try {
      await api("/api/provider/security-questions", {
        method: "POST",
        body: JSON.stringify({ current_password: currentPassword, questions }),
      });
      toast.success("Security questions saved");
      setExisting(questions.map((q) => q.question));
      setQuestions(EMPTY);
      setCurrentPassword("");
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  if (existing === null) {
    return <div className="flex justify-center py-12"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>;
  }

  return (
    <div className="max-w-xl space-y-4">
      <div>
        <h1 className="text-lg font-semibold flex items-center gap-2">
          <KeyRound className="w-5 h-5 text-primary" /> Security questions
        </h1>
        <p className="text-xs text-muted-foreground">
          Your password-reset questions. All three must be answered correctly to reset, so pick
          questions only you can answer — nothing guessable from public information.
        </p>
      </div>

      {existing.length > 0 ? (
        <div className="bg-card rounded-xl border border-border p-4 space-y-1">
          <p className="text-xs font-medium">Current questions</p>
          {existing.map((q, i) => <p key={i} className="text-sm text-muted-foreground">{i + 1}. {q}</p>)}
        </div>
      ) : (
        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-2.5">
          You haven't set security questions yet, so you can't reset your own password if you
          forget it. Set them below.
        </p>
      )}

      <div className="bg-card rounded-xl border border-border p-4 space-y-3">
        <p className="text-xs font-medium">{existing.length ? "Replace all three" : "Set your three questions"}</p>
        {questions.map((pair, index) => (
          <div key={index} className="space-y-1.5">
            <Input
              aria-label={`Security question ${index + 1}`}
              placeholder={`Question ${index + 1}`}
              value={pair.question}
              onChange={(e) => setQuestions(questions.map((q, qi) => qi === index ? { ...q, question: e.target.value } : q))}
            />
            <Input
              aria-label={`Answer ${index + 1}`}
              placeholder="Answer (not case-sensitive)"
              value={pair.answer}
              onChange={(e) => setQuestions(questions.map((q, qi) => qi === index ? { ...q, answer: e.target.value } : q))}
              autoComplete="off"
            />
          </div>
        ))}
        <div>
          <Label htmlFor="sec_current" className="text-xs">Current password (required to save)</Label>
          <Input id="sec_current" type="password" className="mt-1" value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)} autoComplete="current-password" />
        </div>
        <Button onClick={save}
          disabled={busy || !currentPassword
            || questions.some((q) => q.question.trim().length < 8 || q.answer.trim().length < 2)}>
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : "Save questions"}
        </Button>
      </div>
    </div>
  );
}
