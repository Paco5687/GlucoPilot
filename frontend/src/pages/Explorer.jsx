import { useEffect, useRef, useState, useCallback } from "react";
import { base44 } from "@/api/base44Client";
import { Loader2, LineChart } from "lucide-react";

// Port of the original canvas-based Blood Glucose + Insulin Explorer,
// fed from the GlucoseReading/Treatment entity store instead of data.json.

const PAD = { l: 58, r: 22, t: 22, b: 68 };
const fmt = (ms) => new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const shortFmt = (ms) => new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "numeric" });
const timeFmt = (ms) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const num = (v, d = 1) => (v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(d));

function localInputValue(ms) {
  const d = new Date(ms), pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function insulinRemaining(age, dur, peak) {
  if (age <= 0) return 1;
  if (age >= dur) return 0;
  const p = Math.min(Math.max(peak, 1), dur - 1);
  if (age <= p) return 1 - (age * age) / (p * dur);
  return ((dur - age) * (dur - age)) / (dur * (dur - p));
}

async function loadChartData() {
  const [readings, treatments] = await Promise.all([
    base44.entities.GlucoseReading.filter({}, "timestamp", 100000),
    base44.entities.Treatment.filter({}, "timestamp", 100000),
  ]);
  const parseT = (v) => new Date(v).getTime();

  const timeline = readings
    .filter((r) => r.value != null && r.timestamp)
    .map((r) => ({ ms: parseT(r.timestamp), bg: r.value, source: r.source }))
    .sort((a, b) => a.ms - b.ms);
  for (let i = 0; i < timeline.length; i++) {
    timeline[i].prevBg = i > 0 ? timeline[i - 1].bg : null;
    timeline[i].prevMs = i > 0 ? timeline[i - 1].ms : null;
    timeline[i].nextBg = i < timeline.length - 1 ? timeline[i + 1].bg : null;
    timeline[i].nextMs = i < timeline.length - 1 ? timeline[i + 1].ms : null;
  }

  const byType = (t) => treatments.filter((x) => x.type === t && x.timestamp);

  const carbEvents = byType("carb").map((t) => ({ ms: parseT(t.timestamp), amount: Number(t.amount) || 0 }));
  const boluses = byType("insulin")
    .map((t) => {
      const ms = parseT(t.timestamp);
      // attach carbs logged within ±10 min (they were one row in the old CSV data)
      const carbs = carbEvents
        .filter((c) => Math.abs(c.ms - ms) <= 10 * 60000)
        .reduce((s, c) => s + c.amount, 0);
      return {
        ms,
        amount: Number(t.amount) || 0,
        carbs,
        description: t.event_type || "Bolus",
        details: t.notes || "",
        unit: "U",
        type: "insulin",
        insulin_type: t.insulin_type,
        source: t.source,
      };
    })
    .sort((a, b) => a.ms - b.ms);

  const basalEvents = [...byType("tempbasal"), ...byType("suspension")]
    .map((t) => ({
      ms: parseT(t.timestamp),
      amount: Number(t.absolute ?? t.amount) || 0,
      duration: Number(t.duration) || 0,
      description: t.type === "suspension" ? "Suspend" : "Temporary basal",
      details: t.notes || "",
      type: t.type,
      source: t.source,
    }))
    .sort((a, b) => a.ms - b.ms);

  const alarms = byType("note")
    .filter((t) => /alarm|alert/i.test(t.event_type || ""))
    .map((t) => ({ ms: parseT(t.timestamp), description: t.event_type, details: t.notes || "", type: "alarm" }));

  const manualBg = byType("bg")
    .map((t) => ({ ms: parseT(t.timestamp), amount: Number(t.glucose) || 0, description: "Manual BG", type: "bg" }))
    .filter((t) => t.amount > 0);

  const timelineBoluses = boluses.map((b) => ({ ms: b.ms, amount: b.amount, carbs: b.carbs }));

  const allMs = [...timeline.map((d) => d.ms), ...boluses.map((b) => b.ms)];
  const bounds = allMs.length
    ? { start: Math.min(...timeline.slice(0, 1).map((d) => d.ms), allMs[0]), end: Math.max(...allMs) }
    : { start: Date.now() - 864e5, end: Date.now() };

  return { timeline, boluses, timelineBoluses, basalEvents, alarms, manualBg, bounds, counts: { readings: timeline.length, treatments: treatments.length } };
}

export default function Explorer() {
  const canvasRef = useRef(null);
  const shellRef = useRef(null);
  const tooltipRef = useRef(null);
  const dataRef = useRef(null);
  const viewRef = useRef({ start: 0, end: 1 });
  const hoverRef = useRef(null);
  const dragRef = useRef(null);
  const optsRef = useRef(null);

  const [loading, setLoading] = useState(true);
  const [empty, setEmpty] = useState(false);
  const [toggles, setToggles] = useState({ showBolus: true, showBasal: true, showAlarms: true, showManual: true });
  const [iobHours, setIobHours] = useState(4);
  const [peakMinutes, setPeakMinutes] = useState(75);
  const [includeBasalIob, setIncludeBasalIob] = useState(true);
  const [inputs, setInputs] = useState({ start: "", end: "" });
  const [hoverHtml, setHoverHtml] = useState("");
  const [eventHtml, setEventHtml] = useState("");
  const [rangeLabel, setRangeLabel] = useState("");
  const [counts, setCounts] = useState(null);

  optsRef.current = { toggles, iobHours, peakMinutes, includeBasalIob };

  // ───── data-derived helpers (operate on refs so canvas handlers stay stable) ─────

  const visible = useCallback((list) => {
    const { start, end } = viewRef.current;
    return list.filter((d) => d.ms >= start && d.ms <= end);
  }, []);

  const nearestRecord = useCallback((ms) => {
    const timeline = dataRef.current.timeline;
    if (!timeline.length) return null;
    let lo = 0, hi = timeline.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (timeline[mid].ms < ms) lo = mid + 1;
      else hi = mid;
    }
    const a = timeline[lo], b = timeline[Math.max(0, lo - 1)];
    return !b || Math.abs(a.ms - ms) < Math.abs(b.ms - ms) ? a : b;
  }, []);

  const estimateIob = useCallback((ms) => {
    const { iobHours, peakMinutes, includeBasalIob } = optsRef.current;
    const dur = iobHours * 3600e3;
    const peak = peakMinutes * 60000;
    const { timelineBoluses, basalEvents } = dataRef.current;
    let bolusTotal = 0;
    for (let i = timelineBoluses.length - 1; i >= 0; i--) {
      const e = timelineBoluses[i];
      if (e.ms > ms) continue;
      const age = ms - e.ms;
      if (age > dur) break;
      bolusTotal += e.amount * insulinRemaining(age, dur, peak);
    }
    let basalDelta = 0;
    if (includeBasalIob) {
      const scheduledRateBefore = (t) => {
        let rate = null;
        for (const e of basalEvents) {
          if (e.ms > t) break;
          if (/Scheduled/i.test(e.description || "")) rate = e.amount;
        }
        return rate;
      };
      const step = 5 * 60000;
      const startLimit = ms - dur;
      for (const e of basalEvents) {
        if (e.ms > ms) break;
        if (!/Temporary|Suspend/i.test(e.description || "") || !e.duration) continue;
        const start = Math.max(e.ms, startLimit);
        const end = Math.min(e.ms + e.duration * 60000, ms);
        if (end <= start) continue;
        const scheduled = scheduledRateBefore(e.ms);
        if (scheduled == null) continue;
        const deliveredRate = /Suspend/i.test(e.description || "") ? 0 : e.amount;
        const deltaRate = deliveredRate - scheduled;
        for (let t = start; t < end; t += step) {
          const sliceMs = Math.min(step, end - t);
          basalDelta += deltaRate * (sliceMs / 3600e3) * insulinRemaining(ms - (t + sliceMs / 2), dur, peak);
        }
      }
    }
    return { bolus: Math.max(0, bolusTotal), basalDelta, total: bolusTotal + basalDelta };
  }, []);

  const bolusSumWindow = useCallback((ms, hours) => {
    const start = ms - hours * 3600e3;
    let total = 0, count = 0, carbs = 0;
    const { timelineBoluses } = dataRef.current;
    for (let i = timelineBoluses.length - 1; i >= 0; i--) {
      const e = timelineBoluses[i];
      if (e.ms > ms) continue;
      if (e.ms < start) break;
      total += e.amount;
      carbs += e.carbs || 0;
      count += 1;
    }
    return { total, count, carbs };
  }, []);

  const lastBolusBefore = useCallback((ms) => {
    const { timelineBoluses } = dataRef.current;
    for (let i = timelineBoluses.length - 1; i >= 0; i--) {
      if (timelineBoluses[i].ms <= ms) return timelineBoluses[i];
    }
    return null;
  }, []);

  const activeBasalEvent = useCallback((ms) => {
    let current = null;
    for (const e of dataRef.current.basalEvents) {
      if (e.ms > ms) break;
      if (e.duration && ms <= e.ms + e.duration * 60000) current = e;
    }
    return current;
  }, []);

  // ───── rendering ─────

  const render = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas || !dataRef.current) return;
    const ctx = canvas.getContext("2d");
    const rect = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.floor(rect.width * ratio);
    canvas.height = Math.floor(rect.height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

    const w = rect.width, h = rect.height;
    ctx.clearRect(0, 0, w, h);
    const { start: viewStart, end: viewEnd } = viewRef.current;
    const { toggles } = optsRef.current;
    const { timeline, boluses, basalEvents, alarms, manualBg } = dataRef.current;

    const split = Math.floor(h * 0.78);
    const minY = 40, maxY = 340;
    const x = (ms) => PAD.l + ((ms - viewStart) / (viewEnd - viewStart)) * (w - PAD.l - PAD.r);
    const y = (bg) => PAD.t + ((maxY - bg) / (maxY - minY)) * (split - PAD.t - 18);
    const y2base = split + 22, y2h = h - y2base - PAD.b + 34;

    const rows = timeline.filter((d) => d.ms >= viewStart && d.ms <= viewEnd);

    // grid
    ctx.strokeStyle = "#e2e8f0"; ctx.lineWidth = 1; ctx.font = "12px system-ui, sans-serif"; ctx.fillStyle = "#64748b";
    [70, 100, 140, 180, 240, 300].forEach((v) => {
      const yy = y(v);
      ctx.beginPath(); ctx.moveTo(PAD.l, yy); ctx.lineTo(w - PAD.r, yy); ctx.stroke();
      ctx.fillText(String(v), 12, yy + 4);
    });
    const ticks = Math.max(4, Math.min(16, Math.floor(w / 90)));
    for (let i = 0; i <= ticks; i++) {
      const ms = viewStart + ((viewEnd - viewStart) * i) / ticks, xx = x(ms);
      ctx.beginPath(); ctx.moveTo(xx, PAD.t); ctx.lineTo(xx, h - PAD.b + 34); ctx.stroke();
      ctx.textAlign = "center";
      ctx.fillText(timeFmt(ms), xx, 14);
      ctx.fillText(shortFmt(ms), xx, h - 24);
      ctx.textAlign = "left";
    }
    ctx.fillStyle = "#334155";
    ctx.fillText("Glucose mg/dL", 10, 18);

    // target range band + limit lines
    ctx.fillStyle = "rgba(34,197,94,.13)";
    ctx.fillRect(PAD.l, y(180), w - PAD.l - PAD.r, y(70) - y(180));
    ctx.strokeStyle = "#ef4444";
    [70, 180].forEach((v) => { ctx.beginPath(); ctx.moveTo(PAD.l, y(v)); ctx.lineTo(w - PAD.r, y(v)); ctx.stroke(); });

    // temp basal / suspend bands
    if (toggles.showBasal) {
      for (const e of visible(basalEvents).filter((e) => /Temporary|Suspend/i.test(e.description || ""))) {
        const xx = x(e.ms), ww = Math.max(3, x(e.ms + e.duration * 60000) - xx);
        const suspend = /Suspend/i.test(e.description || "");
        ctx.fillStyle = suspend ? "rgba(220,38,38,.28)" : "rgba(13,148,136,.32)";
        ctx.fillRect(xx, y2base, ww, y2h);
        ctx.strokeStyle = suspend ? "rgba(185,28,28,.95)" : "rgba(15,118,110,.95)";
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(xx, y2base); ctx.lineTo(xx, y2base + y2h); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(xx + ww, y2base); ctx.lineTo(xx + ww, y2base + y2h); ctx.stroke();
        ctx.fillStyle = suspend ? "#991b1b" : "#0f766e";
        if (ww > 46) {
          ctx.font = "bold 11px system-ui, sans-serif";
          const label = suspend ? `Suspend ${Math.round(e.duration)}m` : `Temp ${num(e.amount, 2)} U/hr ${Math.round(e.duration)}m`;
          ctx.save();
          ctx.beginPath(); ctx.rect(xx + 2, y2base + 2, ww - 4, 22); ctx.clip();
          ctx.fillText(label, xx + 5, y2base + 16);
          ctx.restore();
        } else {
          ctx.fillRect(xx + Math.max(1, ww / 2 - 2), y2base + 5, 4, Math.max(18, y2h - 10));
        }
      }
    }

    // CGM trend line (break on >20 min gaps)
    ctx.strokeStyle = "#2563eb"; ctx.lineWidth = 2; ctx.beginPath();
    let open = false, lastMs = null;
    for (const d of rows) {
      const xx = x(d.ms), yy = y(d.bg);
      if (!open || (lastMs != null && d.ms - lastMs > 20 * 60000)) { ctx.moveTo(xx, yy); open = true; }
      else ctx.lineTo(xx, yy);
      lastMs = d.ms;
    }
    ctx.stroke();

    // manual BG diamonds
    if (toggles.showManual) {
      ctx.fillStyle = "#f59e0b";
      for (const e of visible(manualBg)) {
        const xx = x(e.ms), yy = y(e.amount);
        ctx.beginPath(); ctx.moveTo(xx, yy - 6); ctx.lineTo(xx + 6, yy); ctx.lineTo(xx, yy + 6); ctx.lineTo(xx - 6, yy); ctx.closePath(); ctx.fill();
      }
    }

    // bolus triangles
    if (toggles.showBolus) {
      for (const e of visible(boluses)) {
        const xx = x(e.ms), size = Math.min(12, 5 + e.amount * 2);
        ctx.fillStyle = "#7c3aed";
        ctx.beginPath(); ctx.moveTo(xx, split - 8 - size); ctx.lineTo(xx - size, split - 8 + size); ctx.lineTo(xx + size, split - 8 + size); ctx.closePath(); ctx.fill();
        if (e.carbs > 0) { ctx.fillStyle = "#ea580c"; ctx.fillRect(xx - 2, split - 25 - 10, 4, 10); }
      }
    }

    // alarms
    if (toggles.showAlarms) {
      ctx.strokeStyle = "#dc2626"; ctx.lineWidth = 2;
      for (const e of visible(alarms)) {
        const xx = x(e.ms);
        ctx.beginPath(); ctx.moveTo(xx, 24); ctx.lineTo(xx, split - 10); ctx.stroke();
      }
    }

    // IOB estimate line in lower lane
    if (rows.length) {
      const maxIob = 5;
      const yIob = (v) => y2base + ((maxIob - Math.max(-1, Math.min(maxIob, v))) / maxIob) * y2h;
      const step = Math.max(1, Math.ceil(rows.length / 900));
      ctx.save();
      ctx.setLineDash([5, 4]); ctx.strokeStyle = "#9333ea"; ctx.lineWidth = 1.5; ctx.beginPath();
      let iobOpen = false;
      for (let i = 0; i < rows.length; i += step) {
        const d = rows[i], iob = estimateIob(d.ms).total, xx = x(d.ms), yy = yIob(iob);
        if (!iobOpen) { ctx.moveTo(xx, yy); iobOpen = true; } else ctx.lineTo(xx, yy);
      }
      ctx.stroke();
      ctx.restore();
      ctx.fillStyle = "#9333ea";
      ctx.fillText("Active bolus est 0-5U", 72, y2base + 14);
      ctx.fillStyle = "#64748b";
      [0, 2.5, 5].forEach((v) => ctx.fillText(String(v), 26, yIob(v) + 4));
    }

    // axis line
    ctx.strokeStyle = "#94a3b8"; ctx.beginPath(); ctx.moveTo(PAD.l, PAD.t); ctx.lineTo(PAD.l, h - PAD.b + 34); ctx.stroke();

    // hover guide line
    if (hoverRef.current != null) {
      const cx = Math.max(PAD.l, Math.min(w - PAD.r, hoverRef.current));
      ctx.strokeStyle = "rgba(15,23,42,.55)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx, 22); ctx.lineTo(cx, h - 34); ctx.stroke();
    }
  }, [visible, estimateIob]);

  const refreshEventList = useCallback(() => {
    const { boluses, basalEvents, alarms, manualBg } = dataRef.current;
    const ev = [
      ...visible(boluses),
      ...visible(basalEvents).filter((e) => /Temporary|Suspend/i.test(e.description || "")),
      ...visible(alarms),
      ...visible(manualBg),
    ]
      .sort((a, b) => a.ms - b.ms)
      .slice(0, 250);
    setEventHtml(
      ev.length
        ? ev
            .map(
              (e) =>
                `<div class="flex gap-2 py-1.5 border-b border-border/60 text-xs"><span class="text-muted-foreground whitespace-nowrap">${fmt(e.ms)}</span><strong class="whitespace-nowrap">${e.type}</strong><span>${e.description || ""} ${e.amount ? `<span class="text-muted-foreground">${num(e.amount, 2)} ${e.unit || (e.type === "bg" ? "mg/dL" : "")}</span>` : ""}${e.details ? `<br><span class="text-muted-foreground">${e.details}</span>` : ""}</span></div>`
            )
            .join("")
        : '<span class="text-muted-foreground text-xs">No visible events in this window.</span>'
    );
  }, [visible]);

  const setView = useCallback((start, end) => {
    const bounds = dataRef.current.bounds;
    const span = Math.max(15 * 60 * 1000, end - start);
    if (start < bounds.start) { start = bounds.start; end = start + span; }
    if (end > bounds.end) { end = bounds.end; start = Math.max(bounds.start, end - span); }
    viewRef.current = { start, end };
    setInputs({ start: localInputValue(start), end: localInputValue(end) });
    render();
    refreshEventList();
  }, [render, refreshEventList]);

  const updateHover = useCallback((px, py = 80) => {
    const canvas = canvasRef.current;
    const tooltip = tooltipRef.current;
    if (!canvas || !dataRef.current) return;
    const rect = canvas.getBoundingClientRect();
    const { start: viewStart, end: viewEnd } = viewRef.current;
    const clamped = Math.max(PAD.l, Math.min(rect.width - PAD.r, px));
    const ms = viewStart + ((clamped - PAD.l) / (rect.width - PAD.l - PAD.r)) * (viewEnd - viewStart);

    // bolus marker hit-test
    const { toggles, iobHours, peakMinutes } = optsRef.current;
    let hit = null;
    if (toggles.showBolus) {
      const split = Math.floor(rect.height * 0.78);
      const x = (t) => PAD.l + ((t - viewStart) / (viewEnd - viewStart)) * (rect.width - PAD.l - PAD.r);
      let bestDist = Infinity;
      for (const e of visible(dataRef.current.boluses)) {
        const xx = x(e.ms), size = Math.min(12, 5 + e.amount * 2), yy = split - 8;
        const dx = Math.abs(px - xx), dy = Math.abs(py - yy);
        if (dx <= size + 8 && dy <= size + 10) {
          const dist = Math.hypot(dx, dy);
          if (dist < bestDist) { hit = e; bestDist = dist; }
        }
      }
    }

    hoverRef.current = clamped;
    render();

    if (hit) {
      const d = nearestRecord(hit.ms);
      const iob = estimateIob(hit.ms);
      setHoverHtml(`<dl class="hoverGrid">
        <dt>Bolus time</dt><dd>${fmt(hit.ms)}</dd>
        <dt>Amount</dt><dd>${num(hit.amount, 2)} U ${hit.insulin_type ? `(${hit.insulin_type})` : ""}</dd>
        <dt>Type</dt><dd>${hit.description || "—"}</dd>
        <dt>Carbs nearby</dt><dd>${hit.carbs ? `${num(hit.carbs, 0)} g` : "—"}</dd>
        <dt>CGM at time</dt><dd>${d ? num(d.bg, 0) : "—"} mg/dL</dd>
        <dt>Before / after CGM</dt><dd>${d ? `${num(d.prevBg, 0)} → ${num(d.nextBg, 0)}` : "—"} mg/dL</dd>
        <dt>Active bolus est.</dt><dd>${num(iob.bolus, 2)} U</dd>
        <dt>Notes</dt><dd>${hit.details || "—"}</dd>
        <dt>Source</dt><dd>${hit.source || "—"}</dd>
      </dl><div class="text-[11px] text-muted-foreground mt-2">This is the specific bolus under your pointer. Move off the triangle to return to timeline hover.</div>`);
      if (tooltip) {
        tooltip.innerHTML = `<strong>Bolus ${fmt(hit.ms)}</strong><br>${num(hit.amount, 2)} U ${hit.description || ""}<br>Carbs: ${hit.carbs ? num(hit.carbs, 0) + " g" : "—"}<br>Nearby CGM: ${d ? `${num(d.prevBg, 0)} → ${num(d.nextBg, 0)}` : "—"} mg/dL`;
        tooltip.style.left = `${Math.min(rect.width - 300, Math.max(12, px + 18))}px`;
        tooltip.style.top = `${Math.max(12, py + 12)}px`;
        tooltip.classList.remove("hidden");
      }
      return;
    }

    const d = nearestRecord(ms);
    if (!d) return;
    const basal = activeBasalEvent(ms);
    const iob = estimateIob(ms);
    const last = lastBolusBefore(ms);
    const b2 = bolusSumWindow(ms, 2), b4 = bolusSumWindow(ms, 4), b6 = bolusSumWindow(ms, 6);
    const sinceLast = last ? `${Math.round((ms - last.ms) / 60000)} min ago (${num(last.amount, 2)} U)` : "none";
    setHoverHtml(`<dl class="hoverGrid">
      <dt>Time</dt><dd>${fmt(d.ms)}</dd>
      <dt>CGM BG</dt><dd>${num(d.bg, 0)} mg/dL</dd>
      <dt>Before / after</dt><dd>${num(d.prevBg, 0)} → ${num(d.nextBg, 0)} mg/dL</dd>
      <dt>Active bolus est.</dt><dd>${num(iob.bolus, 2)} U</dd>
      <dt>Last bolus</dt><dd>${sinceLast}</dd>
      <dt>Bolus last 2h</dt><dd>${num(b2.total, 2)} U / ${num(b2.carbs, 0)} g carbs</dd>
      <dt>Bolus last 4h</dt><dd>${num(b4.total, 2)} U / ${b4.count} events</dd>
      <dt>Bolus last 6h</dt><dd>${num(b6.total, 2)} U / ${b6.count} events</dd>
      <dt>Temp-basal delta</dt><dd>${num(iob.basalDelta, 2)} U</dd>
      <dt>Active basal event</dt><dd>${basal ? `${basal.description} ${num(basal.amount, 2)} U/hr, ${Math.round(basal.duration)}m` : "none"}</dd>
      <dt>Source</dt><dd>${d.source || "—"}</dd>
    </dl><div class="text-[11px] text-muted-foreground mt-2">Active bolus is an estimate from logged boluses over ${iobHours}h with ${peakMinutes}m peak.</div>`);
    if (tooltip) {
      tooltip.innerHTML = `<strong>${fmt(d.ms)}</strong><br>BG: ${num(d.bg, 0)} mg/dL<br>Active bolus est: ${num(iob.bolus, 2)} U<br>Last bolus: ${sinceLast}<br>Bolus last 4h: ${num(b4.total, 2)} U`;
      tooltip.style.left = `${Math.min(rect.width - 300, Math.max(12, px + 18))}px`;
      tooltip.style.top = `${Math.max(12, py + 12)}px`;
      tooltip.classList.remove("hidden");
    }
  }, [visible, nearestRecord, estimateIob, lastBolusBefore, bolusSumWindow, activeBasalEvent, render]);

  // ───── load data ─────

  useEffect(() => {
    let cancelled = false;
    loadChartData().then((data) => {
      if (cancelled) return;
      dataRef.current = data;
      setCounts(data.counts);
      if (!data.timeline.length) { setEmpty(true); setLoading(false); return; }
      setRangeLabel(`${fmt(data.bounds.start)} — ${fmt(data.bounds.end)}`);
      setLoading(false);
      const end = data.bounds.end;
      viewRef.current = { start: Math.max(data.bounds.start, end - 14 * 864e5), end };
      setInputs({ start: localInputValue(viewRef.current.start), end: localInputValue(end) });
      requestAnimationFrame(() => { render(); refreshEventList(); });
    });
    return () => { cancelled = true; };
  }, [render, refreshEventList]);

  // re-render when toggles/sliders change
  useEffect(() => {
    if (!loading && !empty) { render(); refreshEventList(); }
  }, [toggles, iobHours, peakMinutes, includeBasalIob, loading, empty, render, refreshEventList]);

  // ───── canvas interactions ─────

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || loading || empty) return;

    const onMouseMove = (e) => {
      const rect = canvas.getBoundingClientRect();
      updateHover(e.clientX - rect.left, e.clientY - rect.top);
    };
    const onMouseDown = (e) => {
      if (e.button !== 0) return;
      const rect = canvas.getBoundingClientRect();
      dragRef.current = { x: e.clientX, start: viewRef.current.start, end: viewRef.current.end, rectWidth: rect.width };
      canvas.style.cursor = "grabbing";
    };
    const onWindowMove = (e) => {
      const drag = dragRef.current;
      if (!drag) return;
      e.preventDefault();
      const usable = Math.max(1, drag.rectWidth - PAD.l - PAD.r);
      const span = drag.end - drag.start;
      const shift = (-(e.clientX - drag.x) / usable) * span;
      setView(drag.start + shift, drag.end + shift);
    };
    const onWindowUp = () => {
      if (!dragRef.current) return;
      dragRef.current = null;
      canvas.style.cursor = "";
    };
    const onWheel = (e) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const usable = rect.width - PAD.l - PAD.r;
      if (usable <= 0) return;
      const px = Math.max(PAD.l, Math.min(rect.width - PAD.r, e.clientX - rect.left));
      const { start, end } = viewRef.current;
      const anchorRatio = (px - PAD.l) / usable;
      const anchorTime = start + anchorRatio * (end - start);
      const factor = e.deltaY < 0 ? 0.78 : 1.28;
      const bounds = dataRef.current.bounds;
      const newSpan = Math.max(15 * 60 * 1000, Math.min(bounds.end - bounds.start, (end - start) * factor));
      setView(anchorTime - anchorRatio * newSpan, anchorTime + (1 - anchorRatio) * newSpan);
      updateHover(px, e.clientY - rect.top);
    };
    const onLeave = () => {
      hoverRef.current = null;
      tooltipRef.current?.classList.add("hidden");
      render();
    };
    const onResize = () => render();

    canvas.addEventListener("mousemove", onMouseMove);
    canvas.addEventListener("mousedown", onMouseDown);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("mouseleave", onLeave);
    window.addEventListener("mousemove", onWindowMove);
    window.addEventListener("mouseup", onWindowUp);
    window.addEventListener("resize", onResize);
    return () => {
      canvas.removeEventListener("mousemove", onMouseMove);
      canvas.removeEventListener("mousedown", onMouseDown);
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("mouseleave", onLeave);
      window.removeEventListener("mousemove", onWindowMove);
      window.removeEventListener("mouseup", onWindowUp);
      window.removeEventListener("resize", onResize);
    };
  }, [loading, empty, setView, updateHover, render]);

  const setWindow = (w) => {
    const bounds = dataRef.current.bounds;
    const end = bounds.end;
    const spans = { "6h": 0.25, "24h": 1, "3d": 3, "7d": 7, "14d": 14, "30d": 30 };
    setView(w === "all" ? bounds.start : end - spans[w] * 864e5, end);
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground text-sm">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading glucose data…
      </div>
    );
  }
  if (empty) {
    return (
      <div className="text-center py-16 text-muted-foreground">
        <LineChart className="w-10 h-10 mx-auto mb-3 opacity-40" />
        <p className="text-sm">No glucose data yet. Connect a source on the Connections page or run the legacy import.</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <style>{`.hoverGrid{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:12px}.hoverGrid dt{color:hsl(var(--muted-foreground))}.hoverGrid dd{font-weight:600;margin:0}`}</style>
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h1 className="text-xl font-bold">Explorer</h1>
          <p className="text-sm text-muted-foreground mt-1">{rangeLabel}{counts ? ` · ${counts.readings.toLocaleString()} readings · ${counts.treatments.toLocaleString()} treatments` : ""}</p>
        </div>
        <LineChart className="w-6 h-6 text-primary" />
      </div>

      {/* Controls */}
      <div className="bg-card rounded-xl border border-border p-4 flex flex-wrap items-center gap-x-6 gap-y-3 text-xs">
        <div className="flex items-center gap-1.5">
          {["6h", "24h", "3d", "7d", "14d", "30d", "all"].map((w) => (
            <button
              key={w}
              onClick={() => setWindow(w)}
              className="px-2.5 py-1 rounded-lg font-medium bg-secondary hover:bg-accent transition-colors"
            >
              {w}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1">
            Start
            <input
              type="datetime-local"
              className="border border-border rounded-md px-1.5 py-1 bg-background"
              value={inputs.start}
              onChange={(e) => setView(new Date(e.target.value).getTime(), viewRef.current.end)}
            />
          </label>
          <label className="flex items-center gap-1">
            End
            <input
              type="datetime-local"
              className="border border-border rounded-md px-1.5 py-1 bg-background"
              value={inputs.end}
              onChange={(e) => setView(viewRef.current.start, new Date(e.target.value).getTime())}
            />
          </label>
        </div>
        <div className="flex items-center gap-3">
          {[
            ["showBolus", "Bolus"],
            ["showBasal", "Basal bands"],
            ["showAlarms", "Alarms"],
            ["showManual", "Manual BG"],
          ].map(([key, label]) => (
            <label key={key} className="flex items-center gap-1.5 cursor-pointer">
              <input
                type="checkbox"
                checked={toggles[key]}
                onChange={(e) => setToggles((t) => ({ ...t, [key]: e.target.checked }))}
              />
              {label}
            </label>
          ))}
        </div>
        <div className="flex items-center gap-4">
          <label className="flex items-center gap-2">
            Insulin duration <b>{iobHours}h</b>
            <input type="range" min="2" max="8" step="0.5" value={iobHours} onChange={(e) => setIobHours(Number(e.target.value))} />
          </label>
          <label className="flex items-center gap-2">
            Peak <b>{peakMinutes}m</b>
            <input type="range" min="45" max="120" step="5" value={peakMinutes} onChange={(e) => setPeakMinutes(Number(e.target.value))} />
          </label>
          <label className="flex items-center gap-1.5 cursor-pointer">
            <input type="checkbox" checked={includeBasalIob} onChange={(e) => setIncludeBasalIob(e.target.checked)} />
            Temp-basal IOB
          </label>
        </div>
      </div>

      {/* Chart */}
      <div ref={shellRef} className="relative bg-card rounded-xl border border-border p-2">
        <canvas ref={canvasRef} className="w-full cursor-crosshair" style={{ height: "480px" }} />
        <div
          ref={tooltipRef}
          className="hidden absolute z-10 bg-popover text-popover-foreground border border-border rounded-lg shadow-lg px-3 py-2 text-xs leading-5 pointer-events-none max-w-[280px]"
        />
      </div>

      {/* Legend */}
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground px-1">
        <span><i className="inline-block w-4 h-0.5 bg-blue-600 align-middle mr-1" /> CGM trend</span>
        <span><i className="inline-block w-3 h-3 bg-green-500/20 border border-green-600/40 align-middle mr-1" /> 70–180 mg/dL</span>
        <span><i className="inline-block w-0 h-0 align-middle mr-1" style={{ borderLeft: "5px solid transparent", borderRight: "5px solid transparent", borderBottom: "9px solid #7c3aed" }} /> Bolus</span>
        <span><i className="inline-block w-3 h-3 bg-teal-600/30 align-middle mr-1" /> Temp basal / suspend</span>
        <span><i className="inline-block w-2.5 h-2.5 bg-amber-500 rotate-45 align-middle mr-1" /> Manual BG</span>
        <span><i className="inline-block w-0.5 h-3 bg-red-600 align-middle mr-1" /> Alarm</span>
        <span><i className="inline-block w-4 border-t-2 border-dashed border-purple-600 align-middle mr-1" /> Active bolus estimate</span>
      </div>

      {/* Detail panels */}
      <div className="grid md:grid-cols-2 gap-4">
        <div className="bg-card rounded-xl border border-border p-4">
          <h2 className="font-semibold text-sm mb-2">Hovered Moment</h2>
          <div dangerouslySetInnerHTML={{ __html: hoverHtml || '<span class="text-muted-foreground text-xs">Move over the chart to inspect glucose, boluses, basal, and IOB.</span>' }} />
        </div>
        <div className="bg-card rounded-xl border border-border p-4">
          <h2 className="font-semibold text-sm mb-2">Visible Events</h2>
          <div className="max-h-72 overflow-y-auto" dangerouslySetInnerHTML={{ __html: eventHtml }} />
        </div>
      </div>
    </div>
  );
}
