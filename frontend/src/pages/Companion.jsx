import { useState, useEffect, useRef, useCallback } from "react";
import { base44 } from "@/api/base44Client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import ReactMarkdown from "react-markdown";
import SafetyBanner from "../components/SafetyBanner";
import { MessageCircleHeart, Send, Loader2, Brain, Plus, X, Zap, Sparkles, Trash2, Check, BookMarked } from "lucide-react";
import { toast } from "sonner";

const SUGGESTIONS = [
  "I've been exhausted and foggy lately — what in my data might explain it?",
  "What do my recent labs say about my thyroid and inflammation?",
  "Compare my time in range this week vs last week.",
  "How does my cycle line up with my symptoms and energy?",
];

const MEM_TONE = {
  symptom: "text-rose-600", life_context: "text-blue-600", treatment: "text-violet-600",
  goal: "text-emerald-600", observation: "text-amber-600", preference: "text-teal-600", note: "text-muted-foreground",
};

export default function Companion() {
  const [threads, setThreads] = useState([]);
  const [activeThread, setActiveThread] = useState(null); // thread id, or null for a fresh chat
  const [messages, setMessages] = useState([]);
  const [memories, setMemories] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [searching, setSearching] = useState(false);
  const [showMem, setShowMem] = useState(false);
  const [newMem, setNewMem] = useState("");
  const [tier, setTier] = useState(() => localStorage.getItem("companion_tier") || "default");
  const endRef = useRef(null);

  useEffect(() => { localStorage.setItem("companion_tier", tier); }, [tier]);

  const loadThreads = useCallback(async () => {
    try { const r = await base44.functions.invoke("companion", { action: "threads" }); return r.data?.threads || []; }
    catch { return []; }
  }, []);
  const loadMemories = useCallback(async () => {
    try { const r = await base44.functions.invoke("companion", { action: "memories" }); setMemories(r.data?.memories || []); } catch { /* */ }
  }, []);

  const openThread = useCallback(async (id) => {
    setActiveThread(id);
    setMessages([]);
    if (!id) return;
    try {
      const r = await base44.functions.invoke("companion", { action: "history", thread_id: id });
      setMessages(r.data?.messages || []);
    } catch { /* */ }
  }, []);

  useEffect(() => {
    (async () => {
      const t = await loadThreads();
      setThreads(t);
      if (t.length) openThread(t[0].id); // most recently updated
    })();
    loadMemories();
  }, [loadThreads, loadMemories, openThread]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, busy]);

  function appendToLast(delta) {
    setMessages((m) => {
      const copy = m.slice();
      const last = copy[copy.length - 1];
      copy[copy.length - 1] = { ...last, role: "assistant", content: (last?.content || "") + delta };
      return copy;
    });
  }
  function setLastSources(sources) {
    setMessages((m) => {
      const copy = m.slice();
      const last = copy[copy.length - 1];
      copy[copy.length - 1] = { ...last, role: "assistant", sources };
      return copy;
    });
  }

  function newChat() {
    if (busy) return;
    setActiveThread(null);
    setMessages([]);
    setInput("");
  }

  async function deleteThread(id, e) {
    e?.stopPropagation();
    if (busy) return;
    if (!window.confirm("Delete this conversation? This can't be undone. (Your saved memories are kept.)")) return;
    await base44.functions.invoke("companion", { action: "delete_thread", thread_id: id });
    const t = await loadThreads();
    setThreads(t);
    if (activeThread === id) { setActiveThread(null); setMessages([]); }
    toast.success("Conversation deleted");
  }

  async function send(text) {
    const msg = (text ?? input).trim();
    if (!msg || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", content: msg }, { role: "assistant", content: "" }]);
    setBusy(true);
    setSearching(false);
    let threadId = activeThread;
    try {
      const res = await fetch("/api/companion/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ message: msg, tier, thread_id: threadId }),
      });
      if (!res.ok || !res.body) throw new Error("Companion unavailable");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let got = false;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          let evt;
          try { evt = JSON.parse(line); } catch { continue; }
          if (evt.error) throw new Error(evt.error);
          if (evt.thread) { threadId = evt.thread.id; setActiveThread(evt.thread.id); setThreads((t) => [evt.thread, ...t]); }
          if (evt.searching) setSearching(true);
          if (evt.sources) { setSearching(false); if (evt.sources.length) setLastSources(evt.sources); }
          if (evt.delta) { got = true; appendToLast(evt.delta); }
          if (evt.done) {
            if (evt.remembered?.length) {
              toast.success(`Remembered ${evt.remembered.length} new thing${evt.remembered.length === 1 ? "" : "s"}`);
              loadMemories();
            }
          }
        }
      }
      if (!got) throw new Error("No response");
      loadThreads().then(setThreads); // refresh order/titles
    } catch (err) {
      toast.error(err?.message || "Companion unavailable");
      setMessages((m) => {
        const copy = m.slice();
        const last = copy[copy.length - 1];
        if (last?.role === "assistant" && !last.content) copy[copy.length - 1] = { role: "assistant", content: "_Sorry — I couldn't respond just now._" };
        return copy;
      });
    }
    setBusy(false);
    setSearching(false);
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

  const chatSpan = showMem ? "lg:col-span-2" : "lg:col-span-3";

  return (
    <div className="space-y-4">
      <SafetyBanner />
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2"><MessageCircleHeart className="w-5 h-5 text-primary" /> Companion</h1>
          <p className="text-sm text-muted-foreground mt-1">Chat grounded in your full health data. It remembers what you share and reasons across your records.</p>
        </div>
        <div className="flex items-center gap-2">
          <div className="inline-flex items-center rounded-lg border border-border bg-muted/40 p-0.5" title="Which model answers. Fast = quick; Deep = slower but more thorough.">
            <button onClick={() => setTier("default")} disabled={busy}
              className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors ${tier === "default" ? "bg-card shadow-sm text-foreground" : "text-muted-foreground hover:text-foreground"}`}>
              <Zap className="w-3.5 h-3.5" /> Fast
            </button>
            <button onClick={() => setTier("quality")} disabled={busy}
              className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors ${tier === "quality" ? "bg-card shadow-sm text-foreground" : "text-muted-foreground hover:text-foreground"}`}>
              <Sparkles className="w-3.5 h-3.5" /> Deep
            </button>
          </div>
          <Button variant="outline" size="sm" onClick={() => setShowMem((s) => !s)} className="gap-1.5 text-xs">
            <Brain className="w-3.5 h-3.5" /> Memory ({memories.length})
          </Button>
        </div>
      </div>

      <div className={`grid gap-4 ${showMem ? "lg:grid-cols-4" : "lg:grid-cols-4"}`}>
        {/* Threads sidebar */}
        <div className="bg-card rounded-xl border border-border p-2 flex flex-col lg:col-span-1" style={{ height: "70vh" }}>
          <Button size="sm" onClick={newChat} disabled={busy} className="gap-1.5 mb-2 w-full">
            <Plus className="w-4 h-4" /> New chat
          </Button>
          <div className="flex-1 overflow-y-auto space-y-0.5">
            {threads.length === 0 ? (
              <p className="text-[11px] text-muted-foreground px-2 py-3 text-center">No conversations yet</p>
            ) : threads.map((t) => (
              <div key={t.id} onClick={() => !busy && openThread(t.id)}
                className={`group flex items-center gap-1 px-2.5 py-2 rounded-lg cursor-pointer text-sm transition-colors ${activeThread === t.id ? "bg-primary/10 text-primary" : "hover:bg-muted text-muted-foreground"}`}>
                <span className="truncate flex-1">{t.title || "Untitled"}</span>
                <button onClick={(e) => deleteThread(t.id, e)} title="Delete conversation" className="opacity-40 hover:opacity-100 hover:text-destructive flex-shrink-0 p-0.5"><Trash2 className="w-3.5 h-3.5" /></button>
              </div>
            ))}
          </div>
        </div>

        {/* Chat */}
        <div className={`bg-card rounded-xl border border-border flex flex-col ${chatSpan}`} style={{ height: "70vh" }}>
          <div className="flex-1 overflow-y-auto p-4 space-y-4">
            {messages.length === 0 ? (
              <div className="h-full flex flex-col items-center justify-center text-center gap-4 px-4">
                <MessageCircleHeart className="w-10 h-10 text-primary/40" />
                <p className="text-sm text-muted-foreground max-w-md">Ask me anything about your health — thyroid, hormones, energy, sleep, your cycle, labs, glucose, and more. I can see your records and I'll remember what you tell me.</p>
                <div className="flex flex-col gap-2 w-full max-w-md">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} onClick={() => send(s)} className="text-left text-xs bg-muted/50 hover:bg-muted rounded-lg px-3 py-2 text-muted-foreground">{s}</button>
                  ))}
                </div>
              </div>
            ) : (
              messages.map((m, i) => {
                if (m.role === "assistant" && !m.content) return null;
                const isLast = i === messages.length - 1;
                const streaming = busy && isLast && m.role === "assistant";
                const done = !busy && isLast && m.role === "assistant";
                return (
                  <div key={i} className={`flex flex-col ${m.role === "user" ? "items-end" : "items-start"}`}>
                    <div className={`max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${m.role === "user" ? "bg-primary text-primary-foreground whitespace-pre-wrap" : "bg-muted"}`}>
                      {m.role === "user" ? m.content : (
                        <div className="prose prose-sm max-w-none dark:prose-invert [&>*:first-child]:mt-0 [&>*:last-child]:mb-0 [&_p]:my-1.5 [&_ul]:my-1.5 [&_ol]:my-1.5 [&_table]:text-xs">
                          <ReactMarkdown>{m.content}</ReactMarkdown>
                          {streaming && <span className="inline-block w-[3px] h-[1.05em] ml-0.5 -mb-[0.15em] rounded-sm bg-primary animate-pulse" aria-label="responding" />}
                        </div>
                      )}
                    </div>
                    {m.role === "assistant" && m.sources?.length > 0 && (
                      <div className="mt-1.5 max-w-[85%] flex flex-wrap items-center gap-1.5">
                        <span className="text-[10px] text-muted-foreground inline-flex items-center gap-1"><BookMarked className="w-3 h-3" /> Sources:</span>
                        {m.sources.map((s, si) => (
                          <a key={si} href={s.url} target="_blank" rel="noreferrer" title={`${s.title} · ${s.source}`}
                            className="text-[10px] px-1.5 py-0.5 rounded-full bg-primary/10 text-primary hover:bg-primary/20 inline-flex items-center gap-1">
                            [{si + 1}] {s.source}
                          </a>
                        ))}
                      </div>
                    )}
                    {streaming && <span className="text-[11px] text-muted-foreground mt-1 px-1 inline-flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" /> responding…</span>}
                    {done && <span className="text-[11px] text-muted-foreground/70 mt-1 px-1 inline-flex items-center gap-1"><Check className="w-3 h-3" /> done</span>}
                  </div>
                );
              })
            )}
            {busy && !messages[messages.length - 1]?.content && (
              <div className="flex justify-start"><div className="bg-muted rounded-2xl px-4 py-2.5 text-sm text-muted-foreground inline-flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> {searching ? "searching trusted sources…" : tier === "quality" ? "thinking deeply… (slower model, worth the wait)" : "thinking…"}</div></div>
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
          <div className="bg-card rounded-xl border border-border p-4 flex flex-col lg:col-span-1" style={{ height: "70vh" }}>
            <h3 className="text-sm font-semibold flex items-center gap-2 mb-1"><Brain className="w-4 h-4 text-primary" /> What I remember</h3>
            <p className="text-[11px] text-muted-foreground mb-3">Built from your chats, shared across every conversation. Edit freely — it shapes future answers.</p>
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
