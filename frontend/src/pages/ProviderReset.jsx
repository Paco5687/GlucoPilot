import { useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Loader2, KeyRound, CheckCircle2 } from "lucide-react";

// Public: provider password reset by security questions. All three answers
// must match; failures are throttled server-side. Accounts created before
// questions existed can't self-reset — the owner resets those from Settings.

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

function Shell({ children }) {
  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-4">
      <div className="w-full max-w-md bg-card rounded-2xl border border-border p-6 space-y-4">
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-lg bg-primary flex items-center justify-center">
            <span className="text-primary-foreground font-mono font-bold text-sm">GP</span>
          </div>
          <span className="font-semibold tracking-tight">GlucoPilot</span>
        </div>
        {children}
      </div>
    </div>
  );
}

export default function ProviderReset() {
  const [username, setUsername] = useState("");
  const [questions, setQuestions] = useState(null); // null until looked up
  const [unavailable, setUnavailable] = useState(false);
  const [answers, setAnswers] = useState(["", "", ""]);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  async function lookup() {
    setBusy(true);
    setError("");
    setUnavailable(false);
    try {
      const r = await api(`/api/provider/reset/questions?username=${encodeURIComponent(username.trim())}`);
      if (r.questions?.length) {
        setQuestions(r.questions);
        setAnswers(r.questions.map(() => ""));
      } else {
        setUnavailable(true);
      }
    } catch (err) {
      setError(err.message);
    }
    setBusy(false);
  }

  async function reset() {
    setError("");
    if (password !== confirm) { setError("Passwords don't match."); return; }
    setBusy(true);
    try {
      await api("/api/provider/reset", {
        method: "POST",
        body: JSON.stringify({ username: username.trim(), answers, password }),
      });
      setDone(true);
    } catch (err) {
      setError(err.message);
    }
    setBusy(false);
  }

  if (done) {
    return (
      <Shell>
        <div className="text-center space-y-3 py-4">
          <CheckCircle2 className="w-10 h-10 text-emerald-500 mx-auto" />
          <h1 className="font-semibold">Password updated</h1>
          <Button asChild className="w-full"><Link to="/login">Sign in</Link></Button>
        </div>
      </Shell>
    );
  }

  return (
    <Shell>
      <div>
        <h1 className="font-semibold flex items-center gap-2">
          <KeyRound className="w-4 h-4 text-primary" /> Reset your provider password
        </h1>
        <p className="text-xs text-muted-foreground mt-1">
          Answer your three security questions to choose a new password.
        </p>
      </div>

      {!questions && (
        <div className="space-y-3">
          <div>
            <Label htmlFor="reset_user" className="text-xs">Username</Label>
            <Input id="reset_user" className="mt-1" value={username}
              onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
          </div>
          {unavailable && (
            <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-2">
              Self-service reset isn't available for this account — it may not have security
              questions set. Ask the account owner to reset your password.
            </p>
          )}
          {error && <p className="text-xs text-destructive">{error}</p>}
          <Button onClick={lookup} className="w-full" disabled={busy || username.trim().length < 2}>
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : "Continue"}
          </Button>
          <p className="text-center"><Link to="/login" className="text-xs text-muted-foreground hover:text-foreground">Back to sign in</Link></p>
        </div>
      )}

      {questions && (
        <div className="space-y-3">
          {questions.map((q, index) => (
            <div key={index}>
              <Label className="text-xs">{q}</Label>
              <Input className="mt-1" value={answers[index]} autoComplete="off"
                onChange={(e) => setAnswers(answers.map((a, ai) => ai === index ? e.target.value : a))} />
            </div>
          ))}
          <div>
            <Label htmlFor="reset_pass" className="text-xs">New password (min 8 characters)</Label>
            <Input id="reset_pass" type="password" className="mt-1" value={password}
              onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
          </div>
          <div>
            <Label htmlFor="reset_confirm" className="text-xs">Confirm new password</Label>
            <Input id="reset_confirm" type="password" className="mt-1" value={confirm}
              onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
          </div>
          {error && <p className="text-xs text-destructive">{error}</p>}
          <Button onClick={reset} className="w-full"
            disabled={busy || answers.some((a) => a.trim().length < 2) || password.length < 8 || !confirm}>
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : "Reset password"}
          </Button>
        </div>
      )}
    </Shell>
  );
}
