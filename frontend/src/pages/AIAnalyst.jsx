import { useState, useEffect, useRef } from "react";
import { base44 } from "@/api/base44Client";
import { useViewingData } from "@/hooks/useViewingData";
import SafetyBanner from "../components/SafetyBanner";
import { MessageSquare, Send, Loader2, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import ReactMarkdown from "react-markdown";
import { calculateTimeInRange, calculateAverage, calculateCV, calculateGMI } from "@/lib/glucoseUtils";

const SYSTEM_PROMPT = `You are GlucoPilot's AI Diabetes Analyst. You have FULL ACCESS to the user's real CGM glucose data, treatments, and patterns — provided below in your context. Use the ACTUAL NUMBERS from the data. Never say you don't have access or ask for more data.

CRITICAL RULES:
- ALWAYS reference specific numbers, dates, and calculated values from the provided data
- When asked to compare periods, DO THE MATH using the daily breakdown data provided
- Be direct and specific: "Your avg was 142 on May 1st vs 168 on April 28th" — not "if your average was lower..."
- NEVER hedge with "if" or "assume" when the data is right there — USE IT
- NEVER provide insulin dosing instructions or recommendations
- NEVER present anything as medical advice or tell the user to adjust medication
- Remind users to discuss findings with their healthcare provider
- Focus on concrete pattern analysis, trend identification, and glucose behavior
- Use tables and bullet points for comparisons
- Be concise but data-driven`;

const EXAMPLE_QUESTIONS = [
  "Why am I going high in the afternoons?",
  "How does my sleep affect my glucose?",
  "Summarize my glucose control this week",
  "Does activity improve my time in range?",
];

export default function AIAnalyst() {
  const [conversations, setConversations] = useState([]);
  const [activeConvo, setActiveConvo] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(true);
  const [glucoseContext, setGlucoseContext] = useState("");
  const messagesEndRef = useRef(null);
  const { fetchEntity, isViewingShared, viewingEmail } = useViewingData();

  useEffect(() => {
    async function load() {
      const [convos, readings, patterns, ouraRecords] = await Promise.all([
        fetchEntity("AIConversation", "-created_date", 20),
        fetchEntity("GlucoseReading", "-timestamp", 5000),
        fetchEntity("Pattern", "-created_date", 20),
        fetchEntity("OuraDaily", "-date", 90),
      ]);
      setConversations(convos);

      const treatments = await fetchEntity("Treatment", "-timestamp", 2000);

      // Build context
      const now = Date.now();
      const last24h = readings.filter((r) => now - new Date(r.timestamp).getTime() < 86400000);
      const last7d = readings.filter((r) => now - new Date(r.timestamp).getTime() < 7 * 86400000);
      const tir24 = calculateTimeInRange(last24h);
      const tir7d = calculateTimeInRange(last7d);

      // Timeframes
      const last30d = readings.filter((r) => now - new Date(r.timestamp).getTime() < 30 * 86400000);
      const last90d = readings.filter((r) => now - new Date(r.timestamp).getTime() < 90 * 86400000);
      const tir30 = calculateTimeInRange(last30d);
      const tir90 = calculateTimeInRange(last90d);

      // Recent readings detail (last 12 hours, sampled)
      const last12h = readings.filter((r) => now - new Date(r.timestamp).getTime() < 12 * 3600000);
      const sampledReadings = last12h.filter((_, i) => i % 3 === 0).slice(0, 50);
      const readingsDetail = sampledReadings.map(r => {
        const t = new Date(r.timestamp);
        return `${t.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}: ${r.value} mg/dL ${r.trend || ""}`;
      }).join("\n") || "No recent readings";

      // Daily breakdowns for the full 90 days
      const dailyBuckets = {};
      last90d.forEach(r => {
        const key = new Date(r.timestamp).toISOString().split("T")[0];
        if (!dailyBuckets[key]) dailyBuckets[key] = [];
        dailyBuckets[key].push(r.value);
      });
      const dailyBreakdown = Object.entries(dailyBuckets)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([date, vals]) => {
          const avg = Math.round(vals.reduce((s, v) => s + v, 0) / vals.length);
          const inRange = vals.filter(v => v >= 70 && v <= 180).length;
          const below = vals.filter(v => v < 70).length;
          const above = vals.filter(v => v > 180).length;
          const min = Math.min(...vals);
          const max = Math.max(...vals);
          const stdDev = Math.round(Math.sqrt(vals.reduce((s, v) => s + (v - avg) ** 2, 0) / vals.length));
          return `${date}: avg=${avg}, min=${min}, max=${max}, SD=${stdDev}, TIR=${Math.round(inRange / vals.length * 100)}%, below=${Math.round(below / vals.length * 100)}%, above=${Math.round(above / vals.length * 100)}%, n=${vals.length}`;
        }).join("\n") || "Insufficient data";

      // Weekly averages for trend over 3 months
      const weeklyBuckets = {};
      last90d.forEach(r => {
        const d = new Date(r.timestamp);
        const weekStart = new Date(d);
        weekStart.setDate(d.getDate() - d.getDay());
        const key = weekStart.toISOString().split("T")[0];
        if (!weeklyBuckets[key]) weeklyBuckets[key] = [];
        weeklyBuckets[key].push(r.value);
      });
      const weeklyTrends = Object.entries(weeklyBuckets)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([week, vals]) => {
          const avg = Math.round(vals.reduce((s, v) => s + v, 0) / vals.length);
          const tir = vals.filter(v => v >= 70 && v <= 180).length;
          return `Week of ${week}: avg ${avg} mg/dL, TIR ${Math.round(tir / vals.length * 100)}%, ${vals.length} readings`;
        }).join("\n") || "Insufficient data";

      // Recent treatments (last 48h)
      const recentTreatments = treatments.filter(t => now - new Date(t.timestamp).getTime() < 48 * 3600000);
      const treatmentDetail = recentTreatments.slice(0, 50).map(t => {
        const time = new Date(t.timestamp);
        const dateStr = `${time.toLocaleDateString("en-US", { month: "short", day: "numeric" })} ${time.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}`;
        if (t.type === "insulin") return `${dateStr}: ${t.amount}u insulin (${t.insulin_type || "unknown"})`;
        if (t.type === "carb") return `${dateStr}: ${t.amount}g carbs`;
        if (t.type === "bg") return `${dateStr}: BG check ${t.glucose} mg/dL`;
        return `${dateStr}: ${t.type}${t.notes ? " - " + t.notes : ""}`;
      }).join("\n") || "No recent treatments";

      const viewingNote = isViewingShared ? `\nNOTE: You are analyzing data for a shared account (${viewingEmail}), not the user's own data.` : "";

      const ctx = `USER'S GLUCOSE DATA CONTEXT:${viewingNote}

SUMMARY STATS:
Last 24h: ${last24h.length} readings, avg ${calculateAverage(last24h)} mg/dL, TIR ${tir24.inRange}%, above ${tir24.above}%, below ${tir24.below}%, CV ${calculateCV(last24h)}%
Last 7d: ${last7d.length} readings, avg ${calculateAverage(last7d)} mg/dL, TIR ${tir7d.inRange}%, above ${tir7d.above}%, below ${tir7d.below}%, CV ${calculateCV(last7d)}%, GMI ${calculateGMI(last7d)}%
Last 30d: ${last30d.length} readings, avg ${calculateAverage(last30d)} mg/dL, TIR ${tir30.inRange}%, above ${tir30.above}%, below ${tir30.below}%, CV ${calculateCV(last30d)}%, GMI ${calculateGMI(last30d)}%
Last 90d: ${last90d.length} readings, avg ${calculateAverage(last90d)} mg/dL, TIR ${tir90.inRange}%, above ${tir90.above}%, below ${tir90.below}%, CV ${calculateCV(last90d)}%, GMI ${calculateGMI(last90d)}%
Current glucose: ${readings[0]?.value || "N/A"} mg/dL (${readings[0]?.trend || "unknown"} trend)

DAILY BREAKDOWN (last 3 months — use this for day-by-day and period comparisons):
${dailyBreakdown}

WEEKLY TRENDS (last 3 months):
${weeklyTrends}

RECENT READINGS (last 12 hours):
${readingsDetail}

RECENT TREATMENTS (last 48 hours):
${treatmentDetail}

Active patterns: ${patterns.map((p) => `${p.title} (${p.confidence} confidence)`).join("; ") || "None detected"}

OURA RING DATA (sleep, readiness, activity, heart rate — last 90 days):
${ouraRecords.length > 0 ? ouraRecords.slice(0, 60).map(d => {
  const parts = [`${d.date}:`];
  if (d.sleep_score != null) parts.push(`Sleep=${d.sleep_score}`);
  if (d.sleep_total_seconds != null) parts.push(`SleepHrs=${(d.sleep_total_seconds / 3600).toFixed(1)}`);
  if (d.readiness_score != null) parts.push(`Readiness=${d.readiness_score}`);
  if (d.activity_score != null) parts.push(`Activity=${d.activity_score}`);
  if (d.activity_steps != null) parts.push(`Steps=${d.activity_steps}`);
  if (d.average_heart_rate != null) parts.push(`AvgHR=${d.average_heart_rate}`);
  if (d.lowest_heart_rate != null) parts.push(`LowHR=${d.lowest_heart_rate}`);
  return parts.join(" ");
}).join("\n") : "No Oura data available"}

IMPORTANT: When the user asks about relationships between sleep/activity/readiness and glucose, cross-reference the OURA RING DATA dates with the DAILY BREAKDOWN dates above. Look for patterns like: does better sleep correlate with better TIR? Does high activity correlate with lower averages?`;

      setGlucoseContext(ctx);

      if (convos.length > 0) {
        setActiveConvo(convos[0]);
        try {
          setMessages(JSON.parse(convos[0].messages || "[]"));
        } catch {
          setMessages([]);
        }
      }
      setLoading(false);
    }
    load();
  }, [isViewingShared, viewingEmail]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function createNewConversation() {
    const convo = await base44.entities.AIConversation.create({
      title: "New Conversation",
      messages: "[]",
      is_archived: false,
    });
    setConversations((prev) => [convo, ...prev]);
    setActiveConvo(convo);
    setMessages([]);
  }

  async function selectConversation(convo) {
    setActiveConvo(convo);
    try {
      setMessages(JSON.parse(convo.messages || "[]"));
    } catch {
      setMessages([]);
    }
  }

  async function sendMessage(text) {
    if (!text.trim() || sending) return;

    let convo = activeConvo;
    if (!convo) {
      convo = await base44.entities.AIConversation.create({
        title: text.slice(0, 50),
        messages: "[]",
        is_archived: false,
      });
      setConversations((prev) => [convo, ...prev]);
      setActiveConvo(convo);
    }

    const userMsg = { role: "user", content: text };
    const newMsgs = [...messages, userMsg];
    setMessages(newMsgs);
    setInput("");
    setSending(true);

    const conversationHistory = newMsgs
      .slice(-10)
      .map((m) => `${m.role === "user" ? "User" : "Analyst"}: ${m.content}`)
      .join("\n\n");

    const response = await base44.integrations.Core.InvokeLLM({
      prompt: `${SYSTEM_PROMPT}\n\n${glucoseContext}\n\nCONVERSATION:\n${conversationHistory}\n\nRespond as GlucoPilot's AI Analyst. Use the ACTUAL DATA above — cite specific numbers, dates, and computed values. Never say you lack data or hedge with assumptions.`,
      model: "claude_sonnet_4_6",
    });

    const assistantMsg = { role: "assistant", content: response };
    const finalMsgs = [...newMsgs, assistantMsg];
    setMessages(finalMsgs);

    await base44.entities.AIConversation.update(convo.id, {
      messages: JSON.stringify(finalMsgs),
      title: convo.title === "New Conversation" ? text.slice(0, 50) : convo.title,
      context_summary: glucoseContext.slice(0, 500),
    });

    setSending(false);
  }

  async function deleteConversation(convoId) {
    await base44.entities.AIConversation.delete(convoId);
    setConversations((prev) => prev.filter((c) => c.id !== convoId));
    if (activeConvo?.id === convoId) {
      setActiveConvo(null);
      setMessages([]);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-6 h-6 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <SafetyBanner />

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold">AI Diabetes Analyst</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Ask questions about your glucose patterns and behavior
          </p>
        </div>
        <Button onClick={createNewConversation} size="sm" className="gap-2">
          <Plus className="w-4 h-4" /> New Chat
        </Button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4" style={{ minHeight: 500 }}>
        {/* Sidebar */}
        <div className="lg:col-span-1 bg-card rounded-xl border border-border p-3 space-y-1 overflow-y-auto max-h-[600px]">
          <p className="text-xs font-medium text-muted-foreground px-2 py-1">Conversations</p>
          {conversations.map((c) => (
            <div
              key={c.id}
              className={`flex items-center justify-between group px-3 py-2 rounded-lg cursor-pointer transition-colors ${
                activeConvo?.id === c.id ? "bg-primary/10 text-primary" : "hover:bg-accent text-muted-foreground"
              }`}
              onClick={() => selectConversation(c)}
            >
              <span className="text-sm truncate flex-1">{c.title}</span>
              <button
                onClick={(e) => { e.stopPropagation(); deleteConversation(c.id); }}
                className="opacity-0 group-hover:opacity-100 p-1 hover:text-destructive transition-opacity"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
          {conversations.length === 0 && (
            <p className="text-xs text-muted-foreground px-2 py-4 text-center">No conversations yet</p>
          )}
        </div>

        {/* Chat area */}
        <div className="lg:col-span-3 bg-card rounded-xl border border-border flex flex-col">
          {/* Messages */}
          <div className="flex-1 overflow-y-auto p-4 space-y-4 min-h-[400px]">
            {messages.length === 0 && (
              <div className="flex flex-col items-center justify-center h-full text-center py-12">
                <MessageSquare className="w-10 h-10 text-muted-foreground mb-3" />
                <h3 className="font-semibold mb-1">Ask about your glucose data</h3>
                <p className="text-sm text-muted-foreground mb-6 max-w-md">
                  I can analyze your patterns, explain trends, and help you understand your glucose behavior.
                </p>
                <div className="flex flex-wrap gap-2 justify-center">
                  {EXAMPLE_QUESTIONS.map((q, i) => (
                    <button
                      key={i}
                      onClick={() => sendMessage(q)}
                      className="px-3 py-2 bg-secondary rounded-lg text-xs font-medium hover:bg-accent transition-colors"
                    >
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((msg, i) => (
              <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
                <div className={`max-w-[80%] rounded-2xl px-4 py-3 ${
                  msg.role === "user"
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted"
                }`}>
                  {msg.role === "user" ? (
                    <p className="text-sm">{msg.content}</p>
                  ) : (
                    <ReactMarkdown className="text-sm prose prose-sm max-w-none [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
                      {msg.content}
                    </ReactMarkdown>
                  )}
                </div>
              </div>
            ))}

            {sending && (
              <div className="flex justify-start">
                <div className="bg-muted rounded-2xl px-4 py-3">
                  <Loader2 className="w-4 h-4 animate-spin text-muted-foreground" />
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* Input */}
          <div className="border-t border-border p-3">
            <form
              onSubmit={(e) => { e.preventDefault(); sendMessage(input); }}
              className="flex items-center gap-2"
            >
              <input
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="Ask about your glucose data..."
                className="flex-1 bg-secondary rounded-lg px-4 py-2.5 text-sm outline-none focus:ring-2 focus:ring-primary/30"
                disabled={sending}
              />
              <Button type="submit" size="sm" disabled={!input.trim() || sending}>
                <Send className="w-4 h-4" />
              </Button>
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}