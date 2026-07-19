import { useState, useEffect, useRef, useCallback } from "react";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import SafetyBanner from "../components/SafetyBanner";
import { MessageCircleHeart, Send, Loader2, Brain, Plus, X } from "lucide-react";
import { toast } from "sonner";

const SUGGESTIONS = [
  "How has my time in range been lately, and does anything explain the changes?",
  "I've been feeling exhausted and foggy this week — does my data show anything?",
  "Compare my hormone labs to how my cycle affects my glucose.",
  "What patterns should I bring up at my next endo appointment?",
];

const MEM_TONE = {
  symptom: "text-rose-600", life_context: "text-blue-600", treatment: "text-violet-600",
  goal: "text-emerald-600", observation: "text-amber-600", preference: "text-teal-600", note: "text-muted-foreground",
};

export default function Companion() {
  const [messages, setMessages] = useState([]);
  const [memories, setMemories] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [showMem, setShowMem] = useState(true);
  const [newMem, setNewMem] = useState("");
  const endRef = useRef(null);

  const loadMemories = useCallback(async () => {
    try { const r = await base44.functions.invoke("companion", { action: "memories" }); setMemories(r.data?.memories || []); } catch { /* */ }
  }, []);

  useEffect(() => {
    base44.functions.invoke("companion", { action: "history" }).then((r) => setMessages(r.data?.messages || [])).catch(() => {});
    loadMemories();
  }, [loadMemories]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, busy]);

  async function send(text) {
    const msg = (text ?? input).trim();
    if (!msg || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", content: msg }]);
    setBusy(true);
    try {
      const res = await base44.functions.invoke("companion", { action: "send", message: msg });
      if (res.data?.error) throw new Error(res.data.error);
      setMessages((m) => [...m, { role: "assistant", content: res.data.reply }]);
      if (res.data.remembered?.length) {
        toast.success(`Remembered ${res.data.remembered.length} new thing${res.data.remembered.length === 1 ? "" : "s"}`);
        loadMemories();
      }
    } catch (err) {
      toast.error(err?.response?.data?.error || err.message || "Companion unavailable");
      setMessages((m) => [...m, { role: "assistant", content: "_Sorry — I couldn't respond just now._" }]);
    }
    setBusy(false);
  }

  async function addMemory() {
    const c = newMem.trim();
    if (!c) return;
    setNewMem("");
    await base44.functions.invoke("companion", { action: "add_memory", content: c });
    loadMemories();
  }
  async function deleteMemory(id) {
    await base44.functions.invoke("companion", { action: "delete_memory", id });
    setMemories((m) => m.filter((x) => x.id !== id));
  }
  async function clearChat() {
    if (!window.confirm("Clear the whole conversation? (Memories are kept.)")) return;
    await base44.functions.invoke("companion", { action: "clear" });
    setMessages([]);
  }

  return (
    <div className="space-y-4">
      <SafetyBanner />
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2"><MessageCircleHeart className="w-5 h-5 text-primary" /> Companion</h1>
          <p className="text-sm text-muted-foreground mt-1">Chat grounded in your full health data. It remembers what you share and reasons across your records.</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => setShowMem((s) => !s)} className="gap-1.5 text-xs">
            <Brain className="w-3.5 h-3.5" /> Memory ({memories.length})
          </Button>
          {messages.length > 0 && <Button variant="ghost" size="sm" onClick={clearChat} className="text-xs text-muted-foreground">Clear chat</Button>}
        </div>
      </div>

      <div className={`grid gap-4 ${showMem ? "lg:grid-cols-3" : "grid-cols-1"}`}>
        {/* Chat */}
        <div className={`bg-card rounded-xl border border-border flex flex-col ${showMem ? "lg:col-span-2" : ""}`} style={{ height: "70vh" }}>
          <div className="flex-1 overflow-y-auto p-4 space-y-4">
            {messages.length === 0 ? (
              <div className="h-full flex flex-col items-center justify-center text-center gap-4 px-4">
                <MessageCircleHeart className="w-10 h-10 text-primary/40" />
                <p className="text-sm text-muted-foreground max-w-md">Ask me anything about your health — I can see your glucose, labs, cycle, wearables, insulin, and records, and I'll remember what you tell me.</p>
                <div className="flex flex-col gap-2 w-full max-w-md">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} onClick={() => send(s)} className="text-left text-xs bg-muted/50 hover:bg-muted rounded-lg px-3 py-2 text-muted-foreground">{s}</button>
                  ))}
                </div>
              </div>
            ) : (
              messages.map((m, i) => (
                <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                  <div className={`max-w-[85%] rounded-2xl px-4 py-2.5 text-sm whitespace-pre-wrap leading-relaxed ${m.role === "user" ? "bg-primary text-primary-foreground" : "bg-muted"}`}>
                    {m.content}
                  </div>
                </div>
              ))
            )}
            {busy && (
              <div className="flex justify-start"><div className="bg-muted rounded-2xl px-4 py-2.5 text-sm text-muted-foreground inline-flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> thinking…</div></div>
            )}
            <div ref={endRef} />
          </div>
          <div className="border-t border-border p-3 flex items-center gap-2">
            <Input value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && send()} placeholder="Ask about your health…" disabled={busy} className="text-sm" />
            <Button size="icon" onClick={() => send()} disabled={busy || !input.trim()}><Send className="w-4 h-4" /></Button>
          </div>
        </div>

        {/* Memory panel */}
        {showMem && (
          <div className="bg-card rounded-xl border border-border p-4 flex flex-col" style={{ height: "70vh" }}>
            <h3 className="text-sm font-semibold flex items-center gap-2 mb-1"><Brain className="w-4 h-4 text-primary" /> What I remember</h3>
            <p className="text-[11px] text-muted-foreground mb-3">Built from your chats. Edit freely — it shapes future answers.</p>
            <div className="flex items-center gap-2 mb-3">
              <Input value={newMem} onChange={(e) => setNewMem(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addMemory()} placeholder="Add something to remember…" className="text-xs h-8" />
              <Button size="icon" variant="outline" onClick={addMemory} className="h-8 w-8 flex-shrink-0"><Plus className="w-4 h-4" /></Button>
            </div>
            <div className="flex-1 overflow-y-auto space-y-1.5">
              {memories.length === 0 ? (
                <p className="text-xs text-muted-foreground italic">Nothing yet — as you chat, I'll note what you're going through here.</p>
              ) : (
                memories.map((m) => (
                  <div key={m.id} className="group flex items-start gap-2 text-xs bg-muted/40 rounded-lg px-2.5 py-1.5">
                    <span className={`mt-0.5 text-[9px] uppercase font-semibold ${MEM_TONE[m.category] || MEM_TONE.note}`}>{(m.category || "note").replace("_", " ")}</span>
                    <span className="flex-1 leading-snug">{m.content}</span>
                    <button onClick={() => deleteMemory(m.id)} className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive flex-shrink-0"><X className="w-3.5 h-3.5" /></button>
                  </div>
                ))
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
