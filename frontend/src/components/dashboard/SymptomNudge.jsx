import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { NotebookPen, X, Moon } from "lucide-react";
import { useAuth } from "@/lib/AuthContext";

// Gentle evening reminder to log today's symptoms. Shows only in the evening,
// only if nothing's been logged today, and only to the owner — dismissible for the day.
const EVENING_HOUR = 17;

export default function SymptomNudge() {
  const { isAdmin } = useAuth();
  const [show, setShow] = useState(false);
  const [today, setToday] = useState("");

  useEffect(() => {
    if (!isAdmin) return;
    if (new Date().getHours() < EVENING_HOUR) return;
    (async () => {
      try {
        const r = await fetch("/api/symptoms?days=1", { credentials: "same-origin" });
        if (!r.ok) return;
        const d = await r.json();
        const t = d.today || "";
        setToday(t);
        const loggedToday = (d.symptoms || []).some((s) => s.entry_date === t);
        const dismissed = localStorage.getItem("symptom_nudge_dismiss") === t;
        if (!loggedToday && !dismissed) setShow(true);
      } catch { /* silent — a reminder is not worth surfacing errors */ }
    })();
  }, [isAdmin]);

  if (!show) return null;

  function dismiss() {
    if (today) localStorage.setItem("symptom_nudge_dismiss", today);
    setShow(false);
  }

  return (
    <div className="flex items-center gap-3 bg-primary/5 border border-primary/20 rounded-xl px-4 py-3">
      <Moon className="w-5 h-5 text-primary flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium">How did today feel?</p>
        <p className="text-xs text-muted-foreground">Take a moment before bed to log any symptoms — it feeds your history, the Companion, and your Visit Report.</p>
      </div>
      <Link to="/symptoms" className="text-xs font-medium text-primary hover:underline whitespace-nowrap inline-flex items-center gap-1 flex-shrink-0">
        <NotebookPen className="w-3.5 h-3.5" /> Log symptoms
      </Link>
      <button onClick={dismiss} title="Dismiss for today" className="text-muted-foreground hover:text-foreground flex-shrink-0"><X className="w-4 h-4" /></button>
    </div>
  );
}
