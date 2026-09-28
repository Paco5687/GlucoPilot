import { useEffect, useRef, useState, useCallback } from "react";
import { base44 } from "@/api/base44Client";
import { Loader2, LineChart, ChevronLeft, ChevronRight, ZoomIn, ZoomOut, SkipForward } from "lucide-react";
import { buildChartData } from "@/lib/explorerData";
import {
  DAY, SPAN_PRESETS, addLocalDays, clampView, dayWindow, durationText, esc, isDayView,
  localDateValue, localInputValue, localMidnight, localMidnightsBetween, matchSpanPreset, niceTicks,
  parseLocalDate, parseLocalDateTime,
} from "@/lib/explorerTime";

// Port of the original canvas-based Blood Glucose + Insulin Explorer,
// fed from the GlucoseReading/Treatment entity store instead of data.json.

const PAD = { l: 58, r: 22, t: 22, b: 68 };
const fmt = (ms) => new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const shortFmt = (ms) => new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "numeric" });
const timeFmt = (ms) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const num = (v, d = 1) => (v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(d));

const dayLabel = (ms) => new Date(ms).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
const rangeText = (start, end) => {
  if (start === localMidnight(start) && end === addLocalDays(start, 1)) return `${dayLabel(start)} · full day`;
  const sameDay = localMidnight(start) === localMidnight(end - 1);
  const s = new Date(start).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const e = sameDay ? timeFmt(end) : new Date(end).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  return `${s} – ${e} · ${durationText(end - start)}`;
};
const TIR_COLOR = (d) => (d.n < 100 ? "bg-muted" : d.tir >= 80 ? "bg-emerald-500/60" : d.tir >= 60 ? "bg-amber-400/70" : "bg-rose-500/60");

function insulinRemaining(age, dur, peak) {
  if (age <= 0) return 1;
  if (age >= dur) return 0;
  const p = Math.min(Math.max(peak, 1), dur - 1);
  if (age <= p) return 1 - (age * age) / (p * dur);
  return ((dur - age) * (dur - age)) / (dur * (dur - p));
}

async function loadChartData() {
  // Newest first: if a row cap is ever reached it drops the oldest history,
  // never the days she is looking at.
  const [readings, treatments] = await Promise.all([
    base44.entities.GlucoseReading.filter({}, "-timestamp", 250000),
    base44.entities.Treatment.filter({}, "-timestamp", 250000),
  ]);
  return buildChartData(readings, treatments);
}

export default function Explorer() {
  const canvasRef = useRef(null);
  const shellRef = useRef(null);
  const tooltipRef = useRef(null);
  const dataRef = useRef(null);
  const viewRef = useRef({ start: 0, end: 1 });
  const hoverRef = useRef(null);
  const optsRef = useRef(null);

  const [loading, setLoading] = useState(true);
  const [empty, setEmpty] = useState(false);
  const [toggles, setToggles] = useState({ showBolus: true, showBasal: true, showAlarms: true, showManual: true });
  const [iobHours, setIobHours] = useState(4);
  const [peakMinutes, setPeakMinutes] = useState(75);
  const [includeBasalIob, setIncludeBasalIob] = useState(true);
  const [draft, setDraft] = useState({ start: "", end: "" });
  const [draftError, setDraftError] = useState("");
  const [nav, setNav] = useState({ span: null, day: "", atLatest: true, atEnd: true, atStart: false, dayView: false });
  const [daily, setDaily] = useState([]);
  const overlayRef = useRef(null);
  const labelRef = useRef(null);
  const stripRef = useRef(null);
  const commitTimer = useRef(null);
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
      const step = 5 * 60000;
      const startLimit = ms - dur;
      // Only events that could still contribute: began within dur + 24 h.
      let lo = 0, hi = basalEvents.length;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (basalEvents[mid].ms <= ms) lo = mid + 1; else hi = mid; }
      for (let i = lo - 1; i >= 0 && basalEvents[i].ms >= startLimit - DAY; i--) {
        const e = basalEvents[i];
        if (!/Temporary|Suspend/i.test(e.description || "") || !e.duration) continue;
        const start = Math.max(e.ms, startLimit);
        const end = Math.min(e.ms + e.duration * 60000, ms);
        if (end <= start) continue;
        const scheduled = e.scheduledRate;
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
    let scheduled = null, temp = null;
    for (const e of dataRef.current.basalEvents) {
      if (e.ms > ms) break;
      if (!e.duration || ms > e.ms + e.duration * 60000) continue;
      if (e.description === "Scheduled basal") scheduled = e;
      else temp = e;
    }
    return temp || scheduled;
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
    const { ticks, step: tickStep } = niceTicks(viewStart, viewEnd, Math.max(4, Math.min(16, Math.floor(w / 90))));
    ctx.textAlign = "center";
    for (const ms of ticks) {
      const xx = x(ms);
      ctx.beginPath(); ctx.moveTo(xx, PAD.t); ctx.lineTo(xx, h - PAD.b + 34); ctx.stroke();
      const midnight = ms === localMidnight(ms);
      ctx.fillText(tickStep >= DAY || midnight ? shortFmt(ms).replace(/,?\s*12\s*AM$/i, "") : timeFmt(ms), xx, h - 24);
    }
    ctx.textAlign = "left";
    // Day dividers: a firmer line at each local midnight, labelled with the
    // day that begins there (and the first partial day at the left edge).
    const midnights = localMidnightsBetween(viewStart, viewEnd);
    const pxPerDay = ((w - PAD.l - PAD.r) * DAY) / (viewEnd - viewStart);
    if (pxPerDay >= 70) {
      ctx.save();
      ctx.strokeStyle = "#94a3b8"; ctx.lineWidth = 1.5; ctx.font = "600 12px system-ui, sans-serif"; ctx.fillStyle = "#334155";
      const titleEnd = 128; // "Glucose mg/dL" occupies the top-left corner
      if (!midnights.length || x(midnights[0]) - titleEnd > 90) ctx.fillText(dayLabel(viewStart), titleEnd, 14);
      for (const m of midnights) {
        const xx = x(m);
        ctx.beginPath(); ctx.moveTo(xx, PAD.t); ctx.lineTo(xx, h - PAD.b + 34); ctx.stroke();
        if (xx + 4 >= titleEnd && xx + 90 < w) ctx.fillText(dayLabel(m), xx + 4, 14);
      }
      ctx.restore();
    }
    ctx.fillStyle = "#334155";
    ctx.fillText("Glucose mg/dL", 10, 18);

    // target range band + limit lines
    ctx.fillStyle = "rgba(34,197,94,.13)";
    ctx.fillRect(PAD.l, y(180), w - PAD.l - PAD.r, y(70) - y(180));
    ctx.strokeStyle = "#ef4444";
    [70, 180].forEach((v) => { ctx.beginPath(); ctx.moveTo(PAD.l, y(v)); ctx.lineTo(w - PAD.r, y(v)); ctx.stroke(); });

    // temp basal / suspend bands (clipped to the plot so none spill past the edge)
    if (toggles.showBasal) {
      ctx.save();
      ctx.beginPath(); ctx.rect(PAD.l, 0, w - PAD.l - PAD.r, h); ctx.clip();
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
          const pct = e.multiplier ? `${e.multiplier >= 1 ? "+" : ""}${Math.round((e.multiplier - 1) * 100)}%` : `${num(e.amount, 2)} U/hr`;
          const label = suspend ? `Suspend ${Math.round(e.duration)}m` : `Temp ${pct} ${Math.round(e.duration)}m`;
          ctx.save();
          ctx.beginPath(); ctx.rect(xx + 2, y2base + 2, ww - 4, 22); ctx.clip();
          ctx.fillText(label, xx + 5, y2base + 16);
          ctx.restore();
        } else {
          ctx.fillRect(xx + Math.max(1, ww / 2 - 2), y2base + 5, 4, Math.max(18, y2h - 10));
        }
      }
      ctx.restore();
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
      const iobLabel = "Active insulin est 0-5U";
      ctx.font = "12px system-ui, sans-serif";
      ctx.fillStyle = "rgba(255,255,255,.88)";
      ctx.fillRect(68, y2base + 2, ctx.measureText(iobLabel).width + 8, 16);
      ctx.fillStyle = "#9333ea";
      ctx.fillText(iobLabel, 72, y2base + 14);
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

    // Live navigation feedback without a React render per frame.
    if (labelRef.current) labelRef.current.textContent = rangeText(viewStart, viewEnd);
    const b = dataRef.current.bounds;
    if (overlayRef.current && b.end > b.start) {
      const left = ((viewStart - b.start) / (b.end - b.start)) * 100;
      const width = ((viewEnd - viewStart) / (b.end - b.start)) * 100;
      overlayRef.current.style.left = `${left}%`;
      overlayRef.current.style.width = `${Math.max(0.6, width)}%`;
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
                `<div class="flex gap-2 py-1.5 border-b border-border/60 text-xs"><span class="text-muted-foreground whitespace-nowrap">${fmt(e.ms)}</span><strong class="whitespace-nowrap">${esc(e.type === "tempbasal_correction" ? "temp basal" : e.type)}</strong><span>${esc(e.description || "")} ${e.multiplier ? `<span class="text-muted-foreground">${esc(`+${Math.round((e.multiplier - 1) * 100)}% for ${Math.round(e.duration)} min`)}</span>` : e.amount ? `<span class="text-muted-foreground">${num(e.amount, 2)} ${esc(e.unit || (e.type === "bg" ? "mg/dL" : ""))}</span>` : ""}${e.details ? `<br><span class="text-muted-foreground">${esc(e.details)}</span>` : ""}</span></div>`
            )
            .join("")
        : '<span class="text-muted-foreground text-xs">No visible events in this window.</span>'
    );
  }, [visible]);

  const commitView = useCallback(() => {
    const data = dataRef.current;
    if (!data) return;
    const { start, end } = viewRef.current;
    setDraft({ start: localInputValue(start), end: localInputValue(end) });
    setDraftError("");
    setNav({
      span: end - start >= data.bounds.end - data.bounds.start - 60000 ? "all" : matchSpanPreset(end - start),
      day: localDateValue(start),
      atLatest: Math.abs(end - data.bounds.latest) < 60000, // "Latest" = newest data
      atEnd: end >= data.bounds.end - 60000, // › stops at now
      atStart: start <= data.bounds.start + 60000,
      dayView: isDayView(start, end, data.bounds.end),
    });
    refreshEventList();
    try {
      const params = new URLSearchParams(window.location.search);
      params.set("from", new Date(start).toISOString());
      params.set("to", new Date(end).toISOString());
      window.history.replaceState(window.history.state, "", `${window.location.pathname}?${params}`);
    } catch { /* URL sync is a convenience */ }
  }, [refreshEventList]);

  // Every navigation path goes through here. Rendering is immediate; the
  // React-side state (inputs, event list, URL) settles once motion stops, so
  // dragging stays smooth.
  const setView = useCallback((start, end, { immediate = false } = {}) => {
    const data = dataRef.current;
    if (!data) return false;
    const next = clampView(start, end, data.bounds);
    if (!next) return false;
    viewRef.current = next;
    render();
    clearTimeout(commitTimer.current);
    if (immediate) commitView();
    else commitTimer.current = setTimeout(commitView, 140);
    return true;
  }, [render, commitView]);

  const showDay = useCallback((ms) => {
    const b = dataRef.current.bounds;
    if (localMidnight(ms) >= b.end) return;
    const w = dayWindow(ms, b.end);
    setView(w.start, w.end, { immediate: true });
  }, [setView]);

  const stepView = useCallback((dir) => {
    const { start, end } = viewRef.current;
    const b = dataRef.current.bounds;
    if (isDayView(start, end, b.end)) return showDay(addLocalDays(start, dir));
    const span = end - start;
    setView(start + dir * span, end + dir * span, { immediate: true });
  }, [setView, showDay]);

  const zoomView = useCallback((factor, anchorRatio = null) => {
    const { start, end } = viewRef.current;
    const b = dataRef.current.bounds;
    const pinned = end >= b.latest - 60000;
    const ratio = anchorRatio ?? (pinned ? 1 : 0.5);
    const anchor = start + ratio * (end - start);
    const span = (end - start) * factor;
    setView(anchor - ratio * span, anchor + (1 - ratio) * span, { immediate: anchorRatio == null });
  }, [setView]);

  const applySpan = useCallback((span) => {
    const { start, end } = viewRef.current;
    const b = dataRef.current.bounds;
    if (span == null) return setView(b.start, b.end, { immediate: true });
    if (end >= b.latest - 60000) return setView(b.latest - span, b.latest, { immediate: true });
    const center = (start + end) / 2;
    setView(center - span / 2, center + span / 2, { immediate: true });
  }, [setView]);

  const goLatest = useCallback(() => {
    const { start, end } = viewRef.current;
    const b = dataRef.current.bounds;
    setView(b.latest - (end - start), b.latest, { immediate: true });
  }, [setView]);

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
        <dt>Amount</dt><dd>${num(hit.amount, 2)} U ${hit.insulin_type ? `(${esc(hit.insulin_type)})` : ""}</dd>
        <dt>Type</dt><dd>${esc(hit.description || "—")}</dd>
        <dt>Carbs nearby</dt><dd>${hit.carbs ? `${num(hit.carbs, 0)} g` : "—"}</dd>
        <dt>CGM at time</dt><dd>${d ? num(d.bg, 0) : "—"} mg/dL</dd>
        <dt>Before / after CGM</dt><dd>${d ? `${num(d.prevBg, 0)} → ${num(d.nextBg, 0)}` : "—"} mg/dL</dd>
        <dt>Active bolus est.</dt><dd>${num(iob.bolus, 2)} U</dd>
        <dt>Notes</dt><dd>${esc(hit.details || "—")}</dd>
        <dt>Source</dt><dd>${esc(hit.source || "—")}</dd>
      </dl><div class="text-[11px] text-muted-foreground mt-2">This is the specific bolus under your pointer. Move off the triangle to return to timeline hover.</div>`);
      if (tooltip) {
        tooltip.innerHTML = `<strong>Bolus ${fmt(hit.ms)}</strong><br>${num(hit.amount, 2)} U ${esc(hit.description || "")}<br>Carbs: ${hit.carbs ? num(hit.carbs, 0) + " g" : "—"}<br>Nearby CGM: ${d ? `${num(d.prevBg, 0)} → ${num(d.nextBg, 0)}` : "—"} mg/dL`;
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
      <dt>Temp-basal extra</dt><dd>${num(iob.basalDelta, 2)} U</dd>
      <dt>Active basal</dt><dd>${basal ? `${esc(basal.description)} ${num(basal.amount, 2)} U/hr${basal.multiplier ? ` (+${Math.round((basal.multiplier - 1) * 100)}%)` : ""}, ${Math.round(basal.duration)}m` : "none"}</dd>
      <dt>Source</dt><dd>${esc(d.source || "—")}</dd>
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
      setDaily(data.daily);
      setLoading(false);
      const params = new URLSearchParams(window.location.search);
      const from = Date.parse(params.get("from") || ""), to = Date.parse(params.get("to") || "");
      const initial = clampView(from, to, data.bounds) || { start: data.bounds.latest - DAY, end: data.bounds.latest };
      viewRef.current = initial;
      requestAnimationFrame(() => setView(initial.start, initial.end, { immediate: true }));
    });
    return () => { cancelled = true; clearTimeout(commitTimer.current); };
  }, [setView]);

  // re-render when toggles/sliders change
  useEffect(() => {
    if (!loading && !empty) { render(); refreshEventList(); }
  }, [toggles, iobHours, peakMinutes, includeBasalIob, loading, empty, render, refreshEventList]);

  // ───── canvas interactions ─────
  // Pointer events cover mouse, touch, and pen. A plain vertical scroll is
  // left to the page; zoom is Ctrl/⌘ + scroll (a trackpad pinch sends the
  // same), a two-finger pinch, double-click, the buttons, or + / −.

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || loading || empty) return;
    const pointers = new Map();
    let drag = null, pinch = null, tap = null;

    const rectOf = () => canvas.getBoundingClientRect();
    const usableWidth = () => Math.max(1, rectOf().width - PAD.l - PAD.r);
    const ratioAt = (px) => Math.min(1, Math.max(0, (px - PAD.l) / usableWidth()));

    const onPointerDown = (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      canvas.setPointerCapture?.(e.pointerId);
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      const { start, end } = viewRef.current;
      if (pointers.size === 1) {
        drag = { x: e.clientX, start, end };
        tap = { x: e.clientX, y: e.clientY };
        canvas.style.cursor = "grabbing";
      } else if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinch = { dist: Math.abs(a.x - b.x) || 1, start, end, ratio: ratioAt((a.x + b.x) / 2 - rectOf().left) };
        drag = null;
        tap = null;
      }
    };
    const onPointerMove = (e) => {
      if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pinch && pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const span = (pinch.end - pinch.start) * (pinch.dist / (Math.abs(a.x - b.x) || 1));
        const anchor = pinch.start + pinch.ratio * (pinch.end - pinch.start);
        setView(anchor - pinch.ratio * span, anchor + (1 - pinch.ratio) * span);
        return;
      }
      if (drag) {
        if (tap && Math.hypot(e.clientX - tap.x, e.clientY - tap.y) > 6) tap = null;
        const shift = (-(e.clientX - drag.x) / usableWidth()) * (drag.end - drag.start);
        setView(drag.start + shift, drag.end + shift);
        return;
      }
      if (e.pointerType === "mouse") {
        const rect = rectOf();
        updateHover(e.clientX - rect.left, e.clientY - rect.top);
      }
    };
    const endPointer = (e, cancelled) => {
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinch = null;
      if (pointers.size === 0) {
        if (tap && !cancelled) {
          const rect = rectOf();
          updateHover(e.clientX - rect.left, e.clientY - rect.top);
        }
        drag = null;
        tap = null;
        canvas.style.cursor = "";
      }
    };
    const onPointerUp = (e) => endPointer(e, false);
    const onPointerCancel = (e) => endPointer(e, true);
    const onWheel = (e) => {
      const rect = rectOf();
      const unit = e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? 800 : 1;
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        const factor = Math.min(4, Math.max(0.25, Math.exp(e.deltaY * unit * 0.0025)));
        zoomView(factor, ratioAt(e.clientX - rect.left));
        updateHover(e.clientX - rect.left, e.clientY - rect.top);
        return;
      }
      const horizontal = e.shiftKey ? e.deltaY : e.deltaX;
      if (Math.abs(horizontal) > (e.shiftKey ? 0 : Math.abs(e.deltaY))) {
        e.preventDefault();
        const { start, end } = viewRef.current;
        const shift = ((horizontal * unit) / usableWidth()) * (end - start);
        setView(start + shift, end + shift);
      }
    };
    const onDblClick = (e) => zoomView(0.5, ratioAt(e.clientX - rectOf().left));
    const onLeave = (e) => {
      if (e.pointerType !== "mouse" || drag) return;
      hoverRef.current = null;
      tooltipRef.current?.classList.add("hidden");
      render();
    };
    const onKey = (e) => {
      const target = e.target;
      const tag = (target?.tagName || "").toLowerCase();
      if (["input", "textarea", "select"].includes(tag) || target?.isContentEditable) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "ArrowLeft") { e.preventDefault(); stepView(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); stepView(1); }
      else if (e.key === "+" || e.key === "=") { e.preventDefault(); zoomView(0.5); }
      else if (e.key === "-" || e.key === "_") { e.preventDefault(); zoomView(2); }
    };
    const onResize = () => render();

    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointercancel", onPointerCancel);
    canvas.addEventListener("pointerleave", onLeave);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("dblclick", onDblClick);
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", onResize);
    return () => {
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("pointercancel", onPointerCancel);
      canvas.removeEventListener("pointerleave", onLeave);
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("dblclick", onDblClick);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onResize);
    };
  }, [loading, empty, setView, zoomView, stepView, updateHover, render]);

  // Overview strip: tap a day to open it; drag the box to slide the window.
  const stripDrag = useRef(null);
  const stripMsAt = (clientX) => {
    const rect = stripRef.current.getBoundingClientRect();
    const b = dataRef.current.bounds;
    return b.start + (Math.min(Math.max(clientX - rect.left, 0), rect.width) / rect.width) * (b.end - b.start);
  };
  const onStripDown = (e) => {
    e.currentTarget.setPointerCapture?.(e.pointerId);
    const { start, end } = viewRef.current;
    stripDrag.current = { x: e.clientX, start, end, inside: (() => { const ms = stripMsAt(e.clientX); return ms >= start && ms <= end; })(), moved: false };
  };
  const onStripMove = (e) => {
    const d = stripDrag.current;
    if (!d || !d.inside) return;
    if (Math.abs(e.clientX - d.x) > 3) d.moved = true;
    if (!d.moved) return;
    const rect = stripRef.current.getBoundingClientRect();
    const b = dataRef.current.bounds;
    const shift = ((e.clientX - d.x) / rect.width) * (b.end - b.start);
    setView(d.start + shift, d.end + shift);
  };
  const onStripUp = (e) => {
    const d = stripDrag.current;
    stripDrag.current = null;
    if (d && !d.moved) showDay(stripMsAt(e.clientX));
  };

  const applyDraft = (e) => {
    e.preventDefault();
    const start = parseLocalDateTime(draft.start), end = parseLocalDateTime(draft.end);
    if (start == null || end == null) { setDraftError("Enter a full start and end date and time."); return; }
    if (end <= start) { setDraftError("The end must be after the start."); return; }
    setView(start, end, { immediate: true });
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

  const bounds = dataRef.current.bounds;

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

      {/* Navigation */}
      <div className="bg-card rounded-xl border border-border p-3 space-y-2 text-xs">
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded-lg border border-border overflow-hidden">
            <button type="button" onClick={() => stepView(-1)} disabled={nav.atStart}
              aria-label={nav.dayView ? "Previous day" : "Back one window"} title="Back (← key)"
              className="px-2 py-1.5 hover:bg-accent disabled:opacity-40"><ChevronLeft className="w-4 h-4" /></button>
            <button type="button" onClick={() => showDay(bounds.end)}
              className="px-2.5 py-1.5 font-medium hover:bg-accent border-x border-border">Today</button>
            <button type="button" onClick={() => stepView(1)} disabled={nav.atEnd}
              aria-label={nav.dayView ? "Next day" : "Forward one window"} title="Forward (→ key)"
              className="px-2 py-1.5 hover:bg-accent disabled:opacity-40"><ChevronRight className="w-4 h-4" /></button>
          </div>
          <input type="date" aria-label="Go to day" value={nav.day}
            min={localDateValue(bounds.start)} max={localDateValue(bounds.end)}
            onChange={(e) => { const t = parseLocalDate(e.target.value); if (t != null) showDay(t); }}
            className="border border-border rounded-md px-2 py-1 bg-background" />
          <div className="inline-flex rounded-lg border border-border overflow-hidden" role="group" aria-label="Window length">
            {SPAN_PRESETS.map(([label, ms]) => (
              <button key={label} type="button" onClick={() => applySpan(ms)} aria-pressed={nav.span === label}
                className={`px-2.5 py-1.5 font-medium ${nav.span === label ? "bg-primary text-primary-foreground" : "hover:bg-accent"}`}>{label}</button>
            ))}
            <button type="button" onClick={() => applySpan(null)} aria-pressed={nav.span === "all"}
              className={`px-2.5 py-1.5 font-medium ${nav.span === "all" ? "bg-primary text-primary-foreground" : "hover:bg-accent"}`}>All</button>
          </div>
          <div className="inline-flex rounded-lg border border-border overflow-hidden">
            <button type="button" onClick={() => zoomView(2)} aria-label="Zoom out" title="Zoom out (− key)"
              className="px-2 py-1.5 hover:bg-accent"><ZoomOut className="w-4 h-4" /></button>
            <button type="button" onClick={() => zoomView(0.5)} aria-label="Zoom in" title="Zoom in (+ key)"
              className="px-2 py-1.5 hover:bg-accent border-l border-border"><ZoomIn className="w-4 h-4" /></button>
          </div>
          <button type="button" onClick={goLatest} disabled={nav.atLatest}
            className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-border font-medium hover:bg-accent disabled:opacity-40">
            <SkipForward className="w-3.5 h-3.5" /> Latest
          </button>
          <span ref={labelRef} className="ml-auto text-sm font-medium tabular-nums" aria-live="polite" />
        </div>
        <form className="flex flex-wrap items-center gap-2" onSubmit={applyDraft}>
          <span className="text-muted-foreground">Custom range</span>
          <input type="datetime-local" aria-label="Range start" value={draft.start}
            onChange={(e) => setDraft((d) => ({ ...d, start: e.target.value }))}
            className="border border-border rounded-md px-1.5 py-1 bg-background" />
          <span className="text-muted-foreground">to</span>
          <input type="datetime-local" aria-label="Range end" value={draft.end}
            onChange={(e) => setDraft((d) => ({ ...d, end: e.target.value }))}
            className="border border-border rounded-md px-1.5 py-1 bg-background" />
          <button type="submit" className="px-2.5 py-1 rounded-md bg-secondary hover:bg-accent font-medium">Go</button>
          {draftError && <span className="text-destructive" role="alert">{draftError}</span>}
        </form>
      </div>

      {/* Chart */}
      <div ref={shellRef} className="relative bg-card rounded-xl border border-border p-2">
        <canvas ref={canvasRef} className="w-full cursor-crosshair select-none" style={{ height: "480px", touchAction: "pan-y" }} />
        <div
          ref={tooltipRef}
          className="hidden absolute z-10 bg-popover text-popover-foreground border border-border rounded-lg shadow-lg px-3 py-2 text-xs leading-5 pointer-events-none max-w-[280px]"
        />
      </div>

      {/* Every day at a glance */}
      <div className="bg-card rounded-xl border border-border px-3 pt-2 pb-1 text-[11px]">
        <div className="flex flex-wrap justify-between gap-2 text-muted-foreground mb-1">
          <span>Every day at a glance — tap a day to open it, drag the box to slide</span>
          <span className="flex items-center gap-2">
            <i className="inline-block w-2.5 h-2.5 rounded-sm bg-emerald-500/60" />≥80% in range
            <i className="inline-block w-2.5 h-2.5 rounded-sm bg-amber-400/70" />60–79%
            <i className="inline-block w-2.5 h-2.5 rounded-sm bg-rose-500/60" />&lt;60%
            <i className="inline-block w-2.5 h-1 bg-rose-700" />≥4% low
          </span>
        </div>
        <div ref={stripRef} className="relative h-7 rounded-md bg-muted/40 cursor-pointer select-none" style={{ touchAction: "none" }}
          onPointerDown={onStripDown} onPointerMove={onStripMove} onPointerUp={onStripUp}
          onPointerCancel={() => { stripDrag.current = null; }} data-testid="overview-strip">
          {daily.map((d) => (
            <div key={d.start} className={`absolute top-0 bottom-0 ${TIR_COLOR(d)}`}
              title={`${dayLabel(d.start)} · ${d.tir}% in range · avg ${d.mean} mg/dL · ${d.lowPct}% low`}
              style={{ left: `${((d.start - bounds.start) / (bounds.end - bounds.start)) * 100}%`, width: `${(DAY / (bounds.end - bounds.start)) * 100}%` }}>
              {d.lowPct >= 4 && <span className="absolute left-0 right-0 bottom-0 h-1 bg-rose-700" />}
            </div>
          ))}
          <div ref={overlayRef} className="absolute -top-0.5 -bottom-0.5 rounded border-2 border-foreground/70 bg-foreground/5 cursor-grab" />
        </div>
        <div className="relative h-4 text-muted-foreground">
          {daily.filter((d) => new Date(d.start).getDate() === 1).map((d) => (
            <span key={d.start} className="absolute top-0.5" style={{ left: `${((d.start - bounds.start) / (bounds.end - bounds.start)) * 100}%` }}>
              {new Date(d.start).toLocaleDateString([], { month: "short" })}
            </span>
          ))}
        </div>
      </div>
      <p className="text-[11px] text-muted-foreground px-1">
        Drag the chart to move · Ctrl/⌘ + scroll, pinch, or double-click to zoom · ← → step a day (or a window) · + − zoom · the address bar keeps your place, so you can bookmark or share a moment.
      </p>

      {/* Display */}
      <div className="bg-card rounded-xl border border-border p-3 flex flex-wrap items-center gap-x-6 gap-y-2 text-xs">
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


      {/* Legend */}
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground px-1">
        <span><i className="inline-block w-4 h-0.5 bg-blue-600 align-middle mr-1" /> CGM trend</span>
        <span><i className="inline-block w-3 h-3 bg-green-500/20 border border-green-600/40 align-middle mr-1" /> 70–180 mg/dL</span>
        <span><i className="inline-block w-0 h-0 align-middle mr-1" style={{ borderLeft: "5px solid transparent", borderRight: "5px solid transparent", borderBottom: "9px solid #7c3aed" }} /> Bolus</span>
        <span><i className="inline-block w-3 h-3 bg-teal-600/30 align-middle mr-1" /> Temp basal / suspend</span>
        <span><i className="inline-block w-2.5 h-2.5 bg-amber-500 rotate-45 align-middle mr-1" /> Manual BG</span>
        <span><i className="inline-block w-0.5 h-3 bg-red-600 align-middle mr-1" /> Alarm</span>
        <span><i className="inline-block w-4 border-t-2 border-dashed border-purple-600 align-middle mr-1" /> Active insulin estimate (bolus + temp basal)</span>
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
