import { useMemo, useState, useEffect } from "react";
import { Input } from "@/components/ui/input";
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, ReferenceArea, CartesianGrid,
} from "recharts";
import { FlaskConical, AlertTriangle, Search, List, LineChart as LineIcon, Grid3x3, ChevronDown, ChevronRight } from "lucide-react";

const ABNORMAL = new Set(["high", "low", "critical", "abnormal"]);

// Lay-term search: typing a topic surfaces every related analyte even when none
// of them literally contains that word (e.g. "mold" → the mycotoxin panels).
// Each key is a topic; its terms are substrings matched against an analyte's
// name, family, and category. Matching is bidirectional (query "thyroid panel"
// matches the "thyroid" topic, and query "thy" does too).
const SEARCH_ALIASES = {
  mold: ["mold", "myco", "toxin", "cirs", "ochratox", "aflatox", "gliotox", "zearal", "trichothec", "roridin", "verrucarin", "enniatin", "citrinin", "chaetoglobosin", "sterigmatocystin", "mycophenolic", "aspergillus", "penicillium"],
  mycotoxin: ["myco", "toxin", "ochratox", "aflatox", "gliotox", "zearal", "roridin", "verrucarin", "enniatin", "citrinin", "chaetoglobosin", "sterigmatocystin", "mycophenolic"],
  thyroid: ["thyroid", "tsh", "t3", "t4", "thyroglobulin", "tpo", "thyroperox", "reverse t"],
  hormone: ["estr", "estradiol", "estrone", "progesterone", "testosterone", "dhea", "lh", "fsh", "prolactin", "cortisol", "pregnenolone", "androstenedione", "shbg"],
  adrenal: ["cortisol", "cortisone", "acth", "dhea", "aldosterone", "pregnenolone"],
  inflammation: ["crp", "c-reactive", "esr", "sed rate", "tgf", "mmp-9", "interleukin", "il-", "homocysteine", "ferritin"],
  gut: ["elastase", "calprotectin", "zonulin", "d-lactate", "colibactin", "hydrogen sulfide", "h2s", "methane", "secretory iga", "occult", "steatocrit", "beta-glucuron"],
  lipid: ["cholesterol", "ldl", "hdl", "triglyc", "apob", "apo b", "lipoprotein", "vldl"],
  cholesterol: ["cholesterol", "ldl", "hdl", "triglyc", "apob", "lipoprotein"],
  liver: ["alt", "ast", "alkaline phos", "alp", "bilirubin", "ggt", "albumin"],
  kidney: ["creatinine", "bun", "egfr", "urea", "cystatin"],
  iron: ["iron", "ferritin", "transferrin", "tibc", "saturation"],
  metal: ["lead", "mercury", "arsenic", "cadmium", "aluminum", "nickel", "thallium"],
  vitamin: ["vitamin", "b12", "folate", "25-oh", "25-hydroxy", "cobalamin", "riboflavin"],
  autoimmune: ["antibod", " ab ", "ana", "gad", "islet", "tpo", "autoimmun"],
  diabetes: ["glucose", "a1c", "hba1c", "insulin", "c-peptide", "gad", "islet"],
  lyme: ["lyme", "borrelia", "blot"],
  cbc: ["wbc", "rbc", "hemoglobin", "hematocrit", "platelet", "neutrophil", "lymphocyte", "monocyte", "eosinophil", "basophil", "mcv", "mch"],
};

function expandQuery(query) {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const terms = new Set([q]);
  for (const [topic, related] of Object.entries(SEARCH_ALIASES)) {
    if (topic.includes(q) || q.includes(topic)) related.forEach((t) => terms.add(t));
  }
  return [...terms];
}

// Recency: how old the latest result is, so stale values aren't read as current.
const AGE_TONE = {
  fresh: "bg-emerald-100 text-emerald-700",
  mid: "bg-muted text-muted-foreground",
  old: "bg-amber-100 text-amber-700",
  stale: "bg-rose-100 text-rose-700",
};
function ageInfo(dateStr) {
  if (!dateStr) return null;
  const d = new Date(dateStr + "T12:00:00");
  if (Number.isNaN(d.getTime())) return null;
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  let label, tone;
  if (days < 45) { label = `${Math.max(1, Math.round(days / 7))}w ago`; tone = "fresh"; }
  else if (days < 365) { label = `${Math.round(days / 30)}mo ago`; tone = days > 240 ? "old" : "mid"; }
  else { label = days < 730 ? "1y+ ago" : `${Math.floor(days / 365)}y+ ago`; tone = "stale"; }
  return { days, label, tone };
}
function AgeBadge({ date }) {
  const a = ageInfo(date);
  if (!a) return null;
  return <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${AGE_TONE[a.tone]}`} title={date}>{a.label}</span>;
}

// --- analyte identity ------------------------------------------------------
// Two-level grouping so related results sit together without dishonest merging:
//   • variant  = one honest trend line (a specific test + unit). Formatting-only
//                name variants merge; Free/Total, specimen, and unit differences
//                stay separate so scales and meaning are preserved.
//   • family   = the base analyte (e.g. "Cortisol") gathering every variant, so
//                serum / salivary / diurnal cortisol are all shown together.

function normUnit(u) {
  const k = (u || "").toLowerCase().replace(/[μµ]/g, "u").replace(/\s+/g, "");
  if (k === "mcg/dl" || k === "ug/dl") return "µg/dL";
  return (u || "").trim();
}
const unitKey = (u) => normUnit(u).toLowerCase();

// Normalize a unit string for conversion matching (µ→u, drop spaces, mcg→ug).
const nu = (u) => (u || "").toLowerCase().replace(/[µμ]/g, "u").replace(/\s+/g, "").replace("mcg", "ug");

// Common lab unit conversions so the same analyte in different units forms one
// trend, and the whole view can flip between conventional (US) and SI units.
// value_si = value_conv * factor.
const UNIT_CONVERSIONS = [
  { match: ["glucose"], conv: "mg/dL", si: "mmol/L", factor: 0.0555 },
  { match: ["cholesterol", "ldl", "hdl", "non-hdl", "non hdl"], conv: "mg/dL", si: "mmol/L", factor: 0.02586 },
  { match: ["triglyceride"], conv: "mg/dL", si: "mmol/L", factor: 0.01129 },
  { match: ["creatinine"], conv: "mg/dL", si: "µmol/L", factor: 88.42 },
  { match: ["urea nitrogen", "bun"], conv: "mg/dL", si: "mmol/L", factor: 0.357 },
  { match: ["uric acid"], conv: "mg/dL", si: "µmol/L", factor: 59.48 },
  { match: ["calcium"], conv: "mg/dL", si: "mmol/L", factor: 0.2495 },
  { match: ["magnesium"], conv: "mg/dL", si: "mmol/L", factor: 0.4114 },
  { match: ["phosphor", "phosphate"], conv: "mg/dL", si: "mmol/L", factor: 0.3229 },
  { match: ["bilirubin"], conv: "mg/dL", si: "µmol/L", factor: 17.1 },
  { match: ["albumin"], conv: "g/dL", si: "g/L", factor: 10 },
  { match: ["total protein", "protein, total"], conv: "g/dL", si: "g/L", factor: 10 },
  { match: ["iron"], conv: "µg/dL", si: "µmol/L", factor: 0.1791 },
  { match: ["25-hydroxy", "vitamin d", "calcidiol"], conv: "ng/mL", si: "nmol/L", factor: 2.496 },
  { match: ["b12", "cobalamin"], conv: "pg/mL", si: "pmol/L", factor: 0.7378 },
  { match: ["folate"], conv: "ng/mL", si: "nmol/L", factor: 2.266 },
  { match: ["free t4", "t4, free", "free thyroxine"], conv: "ng/dL", si: "pmol/L", factor: 12.87 },
  { match: ["total t4", "t4, total", "thyroxine"], conv: "µg/dL", si: "nmol/L", factor: 12.87 },
  { match: ["free t3", "t3, free"], conv: "pg/mL", si: "pmol/L", factor: 1.536 },
  { match: ["testosterone"], conv: "ng/dL", si: "nmol/L", factor: 0.03467 },
  { match: ["estradiol"], conv: "pg/mL", si: "pmol/L", factor: 3.671 },
  { match: ["cortisol"], conv: "µg/dL", si: "nmol/L", factor: 27.59 },
].map((e) => ({ ...e, _c: nu(e.conv), _s: nu(e.si) }));

// The conversion whose analyte name AND unit both fit this lab, else null.
function convFor(name, unit) {
  const n = (name || "").toLowerCase();
  const u = nu(unit);
  for (const e of UNIT_CONVERSIONS) {
    if (e.match.some((m) => n.includes(m)) && (u === e._c || u === e._s)) return e;
  }
  return null;
}

const roundSmart = (v) => {
  const a = Math.abs(v);
  const f = a >= 100 ? 1 : a >= 10 ? 10 : a >= 1 ? 100 : 1000;
  return Math.round(v * f) / f;
};

// Convert a lab reading (and its reference range) into the target unit system.
// Returns null when the analyte/unit isn't in the conversion table.
function convertLab(lab, target) {
  const e = convFor(lab.test_name, lab.unit);
  if (e == null) return null;
  const u = nu(lab.unit);
  const toConv = (x) => (x == null ? null : (u === e._c ? x : x / e.factor)); // → conventional
  const out = (x) => (x == null ? null : roundSmart(target === "si" ? toConv(x) * e.factor : toConv(x)));
  return {
    value: out(lab.value),
    unit: target === "si" ? e.si : e.conv,
    reference_low: out(lab.reference_low),
    reference_high: out(lab.reference_high),
    bucketUnit: e._c, // constant per analyte, so mg/dL & mmol/L variants merge
  };
}

const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

// Collapse the SAME test written different ways into one trend line: ignore
// specimen words, generic filler, and A.M./P.M. draw markers, and be order-
// insensitive. Everything that changes the measurement is KEPT — numbers
// (2-OH-E1 ≠ 4-OH-E1), α/β isomers, letters (SS-A ≠ SS-B), "%", free/total,
// and diurnal time-points — so different tests never merge.
const CANON_STOP = new Set([
  "", "reflex", "w", "with", "level", "levels", "panel",
  "serum", "plasma", "blood", "salivary", "saliva", "urine", "urinary", "whole", "random",
]);
function canonKey(name) {
  return (name || "")
    .toLowerCase()
    .replace(/\b[ap]\.?m\.?\b/g, " ") // drop A.M./P.M./am/pm draw markers (not diurnal panel words)
    .replace(/[.,()/\-]/g, " ")
    .split(/\s+/)
    .filter((t) => t && !CANON_STOP.has(t))
    .sort()
    .join(" ");
}

// The base analyte, for gathering related variants under one heading.
const FAMILY_STOP = new Set([
  "serum", "plasma", "blood", "urine", "urinary", "salivary", "saliva", "whole",
  "fasting", "random", "free", "total", "direct", "indirect", "bioavailable",
  "morning", "afternoon", "evening", "night", "waking", "bed", "am", "pm", "noon",
  "a", "b", "c", "d", "level", "levels", "reflex", "w", "with", "panel",
]);
function familyOf(name) {
  let s = (name || "")
    .toLowerCase()
    .replace(/\([^)]*\)/g, " ")            // drop "(Morning)", "(~5pm)"
    .replace(/~?\d{1,2}\s*[ap]\.?m\.?/g, " ") // drop times like ~5pm
    .replace(/\b[ap]\.?\s*m\.?\b/g, " ");   // drop A.M. / P M
  const base = s.replace(/[.,()/\-]/g, " ").split(/\s+/)
    .filter((t) => t && t.length > 1 && !FAMILY_STOP.has(t) && !/^\d+$/.test(t));
  const label = base.map(cap).join(" ").trim();
  return { key: [...base].sort().join(" ") || (name || "").toLowerCase(), label: label || name };
}
// What distinguishes a variant inside its family (specimen / timepoint / free-total / unit).
function variantOf(name, familyBase, unit) {
  const paren = [...(name || "").matchAll(/\(([^)]*)\)/g)].map((m) => m[1]).join(" ");
  const fam = new Set(familyBase.split(" "));
  const rest = (name || "").replace(/\([^)]*\)/g, " ").replace(/[.,()/\-]/g, " ")
    .split(/\s+/).filter((t) => t && !fam.has(t.toLowerCase()));
  const label = [paren, rest.join(" ")].filter(Boolean).join(" · ").trim();
  return label || normUnit(unit) || "result";
}

// --- data prep -------------------------------------------------------------

function isAbnormal(flag, value, lo, hi) {
  const f = (flag || "").toLowerCase();
  if (ABNORMAL.has(f)) return f;
  if (value != null && lo != null && value < lo) return "low";
  if (value != null && hi != null && value > hi) return "high";
  return "";
}

function useFamilies(labs, units) {
  return useMemo(() => {
    // 1. bucket into variants keyed by (canonical name + unit). Convertible
    //    analytes are normalized to a single unit so mg/dL & mmol/L etc. merge,
    //    and shown in the selected unit system.
    const byVariant = new Map();
    for (const lab of labs) {
      if (lab.value == null || !lab.test_name) continue;
      const c = convertLab(lab, units);
      const bucketUnit = c ? c.bucketUnit : unitKey(lab.unit);
      const point = c ? { ...lab, value: c.value, unit: c.unit, reference_low: c.reference_low, reference_high: c.reference_high } : lab;
      const key = `${canonKey(lab.test_name)}|${bucketUnit}`;
      if (!byVariant.has(key)) byVariant.set(key, []);
      byVariant.get(key).push(point);
    }

    // 2. build a variant record for each
    const variants = [];
    for (const points of byVariant.values()) {
      points.sort((a, b) => String(a.collected_date).localeCompare(String(b.collected_date)));
      const latest = points[points.length - 1];
      const refLow = points.find((p) => p.reference_low != null)?.reference_low ?? null;
      const refHigh = points.find((p) => p.reference_high != null)?.reference_high ?? null;
      // most common raw name = display name
      const freq = {};
      for (const p of points) freq[p.test_name] = (freq[p.test_name] || 0) + 1;
      const name = Object.entries(freq).sort((a, b) => b[1] - a[1])[0][0];
      const fam = familyOf(name);
      variants.push({
        name, points, latest, refLow, refHigh,
        unit: normUnit(latest.unit),
        abnormal: isAbnormal(latest.flag, latest.value, refLow, refHigh),
        count: points.length,
        lastDate: latest.collected_date || "",
        category: latest.category || "Other",
        familyKey: fam.key, familyLabel: fam.label,
        variantLabel: variantOf(name, fam.key, latest.unit),
      });
    }

    // 3. gather variants into families
    const famMap = new Map();
    for (const v of variants) {
      if (!famMap.has(v.familyKey)) {
        famMap.set(v.familyKey, { key: v.familyKey, label: v.familyLabel, variants: [], categories: new Set() });
      }
      const f = famMap.get(v.familyKey);
      f.variants.push(v);
      f.categories.add(v.category);
    }
    const families = [...famMap.values()].map((f) => {
      f.variants.sort((a, b) =>
        (b.abnormal ? 1 : 0) - (a.abnormal ? 1 : 0) ||
        String(b.lastDate).localeCompare(String(a.lastDate)) ||
        a.variantLabel.localeCompare(b.variantLabel)
      );
      f.abnormalCount = f.variants.filter((v) => v.abnormal).length;
      f.lastDate = f.variants.map((v) => v.lastDate).sort().reverse()[0] || "";
      f.category = f.variants[0].category;
      f.count = f.variants.reduce((n, v) => n + v.count, 0);
      return f;
    });
    families.sort((a, b) =>
      (b.abnormalCount ? 1 : 0) - (a.abnormalCount ? 1 : 0) ||
      String(b.lastDate).localeCompare(String(a.lastDate)) ||
      a.label.localeCompare(b.label)
    );
    return families;
  }, [labs, units]);
}

// Whether any analyte in the set can be shown in alternate units (controls the toggle).
function hasConvertible(labs) {
  return labs.some((l) => l.value != null && l.test_name && convFor(l.test_name, l.unit));
}

// --- small pieces ----------------------------------------------------------

function Sparkline({ points, color }) {
  const vals = points.map((p) => p.value);
  if (vals.length < 2) return <span className="text-[10px] text-muted-foreground">1 pt</span>;
  const min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1;
  const W = 80, H = 22;
  const coords = vals.map((v, i) => {
    const x = (i / (vals.length - 1)) * (W - 2) + 1;
    const y = H - 1 - ((v - min) / span) * (H - 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <svg width={W} height={H} className="overflow-visible">
      <polyline points={coords.join(" ")} fill="none" stroke={color} strokeWidth="1.5" />
      <circle cx={coords[coords.length - 1].split(",")[0]} cy={coords[coords.length - 1].split(",")[1]} r="2" fill={color} />
    </svg>
  );
}

function flagBadge(abn) {
  if (!abn) return null;
  const tone = abn === "critical" ? "bg-red-200 text-red-800" : abn === "low" ? "bg-amber-100 text-amber-700" : "bg-red-100 text-red-700";
  return <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${tone} inline-flex items-center gap-0.5`}><AlertTriangle className="w-2.5 h-2.5" />{abn}</span>;
}

function LabTrend({ a }) {
  const data = a.points.map((p) => ({ date: p.collected_date, value: p.value, flag: p.flag, unit: p.unit }));
  const values = a.points.map((p) => p.value).concat(a.refLow ?? [], a.refHigh ?? []);
  const min = Math.min(...values), max = Math.max(...values);
  const pad = (max - min) * 0.15 || Math.abs(max) * 0.1 || 1;
  if (a.points.length < 2) return <p className="text-xs text-muted-foreground italic px-1 py-2">One result so far — a trend appears after the next result.</p>;
  return (
    <div className="h-32 pt-2">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
          {a.refLow != null && a.refHigh != null && (
            <ReferenceArea y1={a.refLow} y2={a.refHigh} fill="hsl(var(--primary))" fillOpacity={0.07} stroke="none" />
          )}
          <XAxis dataKey="date" tick={{ fontSize: 10 }} stroke="hsl(var(--muted-foreground))" tickLine={false} />
          <YAxis domain={[Math.floor((min - pad) * 100) / 100, Math.ceil((max + pad) * 100) / 100]} tick={{ fontSize: 10 }} stroke="hsl(var(--muted-foreground))" tickLine={false} width={44} />
          <Tooltip formatter={(v, _n, item) => [`${v} ${item?.payload?.unit || ""}`, a.name]} contentStyle={{ fontSize: 12, borderRadius: 8 }} />
          <Line type="monotone" dataKey="value" stroke="hsl(var(--primary))" strokeWidth={2}
            dot={({ cx, cy, payload, index }) => {
              const bad = isAbnormal(payload.flag, payload.value, a.refLow, a.refHigh);
              return <circle key={index} cx={cx} cy={cy} r={4} fill={bad ? "#dc2626" : "hsl(var(--primary))"} stroke="hsl(var(--card))" strokeWidth={2} />;
            }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function VariantRow({ v, expanded, onToggle, showLabel }) {
  const stale = ageInfo(v.lastDate)?.tone === "stale";
  return (
    <div>
      <button onClick={onToggle} className={`w-full flex items-center gap-3 px-4 py-2 hover:bg-accent/30 text-left ${stale ? "opacity-60" : ""}`}>
        <span className="flex-1 min-w-0 flex items-center gap-2">
          <span className="text-sm truncate">{showLabel ? v.variantLabel : v.name}</span>
          <AgeBadge date={v.lastDate} />
        </span>
        <Sparkline points={v.points} color={v.abnormal ? "#dc2626" : "hsl(var(--primary))"} />
        <span className="w-24 text-right text-sm font-medium tabular-nums">
          {v.latest.value}<span className="text-[10px] text-muted-foreground ml-0.5">{v.unit}</span>
        </span>
        <span className="w-24 text-right text-[11px] text-muted-foreground tabular-nums hidden sm:block">
          {v.refLow != null && v.refHigh != null ? `${v.refLow}–${v.refHigh}` : "—"}
        </span>
        <span className="w-16 flex justify-end">{flagBadge(v.abnormal)}</span>
      </button>
      {expanded && <div className="px-4 pb-3 bg-accent/10"><LabTrend a={v} /></div>}
    </div>
  );
}

// --- views -----------------------------------------------------------------

function IndexView({ families }) {
  const [expanded, setExpanded] = useState(null);
  const [collapsed, setCollapsed] = useState(() => new Set());
  const toggle = (id) => setExpanded((e) => (e === id ? null : id));

  return (
    <div className="space-y-3">
      {families.map((f) => {
        const multi = f.variants.length > 1;
        const isCol = collapsed.has(f.key);
        return (
          <div key={f.key} className="bg-card rounded-xl border border-border overflow-hidden">
            <button
              onClick={() => setCollapsed((p) => { const n = new Set(p); n.has(f.key) ? n.delete(f.key) : n.add(f.key); return n; })}
              className="w-full flex items-center justify-between px-4 py-2.5 hover:bg-accent/40"
            >
              <span className="flex items-center gap-2 text-sm font-semibold">
                {isCol ? <ChevronRight className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                {f.label}
                {multi && <span className="text-xs font-normal text-muted-foreground">{f.variants.length} tests · {f.count} results</span>}
                {!multi && <AgeBadge date={f.lastDate} />}
              </span>
              {f.abnormalCount > 0 && <span className="text-[10px] px-2 py-0.5 rounded-full bg-red-100 text-red-700 font-medium">{f.abnormalCount} out of range</span>}
            </button>
            {!isCol && (
              <div className="divide-y divide-border border-t border-border">
                {f.variants.map((v) => (
                  <VariantRow key={`${v.name}|${v.unit}`} v={v} showLabel={multi}
                    expanded={expanded === `${f.key}|${v.name}|${v.unit}`}
                    onToggle={() => toggle(`${f.key}|${v.name}|${v.unit}`)} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function ChartsView({ families }) {
  const cards = families.flatMap((f) => f.variants.map((v) => ({ ...v, multi: f.variants.length > 1, familyLabel: f.label })));
  return (
    <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-4">
      {cards.map((a) => (
        <div key={`${a.name}|${a.unit}`} className="bg-card rounded-xl border border-border p-4">
          <div className="flex items-start justify-between gap-2 mb-1">
            <div>
              <h4 className="font-semibold text-sm flex items-center gap-2">{a.multi ? `${a.familyLabel} — ${a.variantLabel}` : a.name} <AgeBadge date={a.lastDate} /></h4>
              <p className="text-xs text-muted-foreground">
                last {a.lastDate || "—"} · {a.count} result{a.count === 1 ? "" : "s"}{a.refLow != null && a.refHigh != null ? ` · ref ${a.refLow}–${a.refHigh} ${a.unit}` : ""}
              </p>
            </div>
            <div className="text-right">
              <div className="text-lg font-bold tabular-nums">{a.latest.value}<span className="text-xs font-normal text-muted-foreground ml-1">{a.unit}</span></div>
              {flagBadge(a.abnormal)}
            </div>
          </div>
          <LabTrend a={a} />
        </div>
      ))}
    </div>
  );
}

function MatrixView({ families }) {
  const rowsIn = families.flatMap((f) => f.variants.map((v) => ({ ...v, multi: f.variants.length > 1, familyLabel: f.label })));
  const { months, rows } = useMemo(() => {
    const monthSet = new Set();
    const rows = rowsIn.map((a) => {
      const cells = new Map();
      for (const p of a.points) {
        const m = String(p.collected_date).slice(0, 7);
        if (m.length !== 7) continue;
        monthSet.add(m);
        cells.set(m, { value: p.value, abn: isAbnormal(p.flag, p.value, a.refLow, a.refHigh), date: p.collected_date });
      }
      return { a, cells };
    });
    return { months: [...monthSet].sort(), rows };
  }, [rowsIn]);

  const color = (abn) => abn === "critical" ? "#b91c1c" : abn === "high" ? "#ef4444" : abn === "low" ? "#f59e0b" : "#10b981";

  if (!months.length) return <p className="text-sm text-muted-foreground">No dated results to chart.</p>;
  return (
    <div className="bg-card rounded-xl border border-border p-3 overflow-x-auto">
      <table className="border-separate border-spacing-0.5">
        <thead>
          <tr>
            <th className="sticky left-0 bg-card z-10" />
            {months.map((m) => (
              <th key={m} className="text-[9px] text-muted-foreground font-normal px-0.5 align-bottom">
                <div className="whitespace-nowrap" style={{ writingMode: "vertical-rl" }}>{m}</div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(({ a, cells }) => {
            const label = a.multi ? `${a.familyLabel} — ${a.variantLabel}` : a.name;
            return (
              <tr key={`${a.name}|${a.unit}`}>
                <td className="sticky left-0 bg-card z-10 text-xs pr-2 whitespace-nowrap max-w-[180px] truncate" title={label}>{label}</td>
                {months.map((m) => {
                  const c = cells.get(m);
                  return (
                    <td key={m} className="p-0">
                      {c ? (
                        <div title={`${label}: ${c.value} ${a.unit} (${c.date})${c.abn ? " — " + c.abn : ""}`}
                          className="w-4 h-4 rounded-sm mx-auto" style={{ backgroundColor: color(c.abn) }} />
                      ) : <div className="w-4 h-4 mx-auto rounded-sm bg-muted/40" />}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="flex items-center gap-3 text-[11px] text-muted-foreground mt-3">
        <span className="inline-flex items-center gap-1"><span className="w-3 h-3 rounded-sm" style={{ background: "#10b981" }} /> in range</span>
        <span className="inline-flex items-center gap-1"><span className="w-3 h-3 rounded-sm" style={{ background: "#ef4444" }} /> high</span>
        <span className="inline-flex items-center gap-1"><span className="w-3 h-3 rounded-sm" style={{ background: "#f59e0b" }} /> low</span>
        <span className="inline-flex items-center gap-1"><span className="w-3 h-3 rounded-sm bg-muted/40" /> no test</span>
      </div>
    </div>
  );
}

// --- container -------------------------------------------------------------

const VIEWS = [
  { key: "index", label: "Index", icon: List },
  { key: "charts", label: "Charts", icon: LineIcon },
  { key: "matrix", label: "Matrix", icon: Grid3x3 },
];

export default function LabsView({ labs }) {
  const [units, setUnits] = useState(() => localStorage.getItem("labs_units") || "conventional");
  const families = useFamilies(labs, units);
  const [view, setView] = useState("index");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("all");
  const [flaggedOnly, setFlaggedOnly] = useState(false);
  const convertible = useMemo(() => hasConvertible(labs), [labs]);

  useEffect(() => { localStorage.setItem("labs_units", units); }, [units]);

  const categories = useMemo(
    () => [...new Set(families.flatMap((f) => f.variants.map((v) => v.category)))].sort(),
    [families]
  );

  const filtered = useMemo(() => {
    const terms = expandQuery(query);
    return families
      .map((f) => {
        const variants = f.variants.filter((v) => {
          const hay = `${v.name} ${f.label} ${v.category}`.toLowerCase();
          return (!terms.length || terms.some((t) => hay.includes(t))) &&
            (category === "all" || v.category === category) &&
            (!flaggedOnly || v.abnormal);
        });
        return { ...f, variants };
      })
      .filter((f) => f.variants.length > 0);
  }, [families, query, category, flaggedOnly]);

  const analyteCount = families.reduce((n, f) => n + f.variants.length, 0);
  const abnormalCount = families.reduce((n, f) => n + f.abnormalCount, 0);
  if (families.length === 0) return null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h2 className="font-semibold text-base flex items-center gap-2">
          <FlaskConical className="w-4 h-4 text-primary" /> Lab trends
          <span className="text-xs font-normal text-muted-foreground">{families.length} analytes · {analyteCount} tests</span>
          {abnormalCount > 0 && <span className="text-[10px] px-2 py-0.5 rounded-full bg-red-100 text-red-700 font-medium">{abnormalCount} out of range</span>}
        </h2>
        <div className="flex items-center gap-2">
          {convertible && (
            <div className="flex rounded-lg border border-border overflow-hidden" title="Show convertible labs in conventional (US) or SI units">
              {[["conventional", "US"], ["si", "SI"]].map(([key, label]) => (
                <button key={key} onClick={() => setUnits(key)}
                  className={`text-xs px-2.5 py-1.5 font-medium ${units === key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}>
                  {label}
                </button>
              ))}
            </div>
          )}
          <div className="flex rounded-lg border border-border overflow-hidden">
            {VIEWS.map((v) => {
              const Icon = v.icon;
              return (
                <button key={v.key} onClick={() => setView(v.key)}
                  className={`text-xs px-3 py-1.5 flex items-center gap-1.5 ${view === v.key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}>
                  <Icon className="w-3.5 h-3.5" /> {v.label}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative flex-1 min-w-[180px]">
          <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search analytes, categories, or topics (e.g. mold, thyroid)…" className="pl-8 h-9 text-sm" />
        </div>
        <select value={category} onChange={(e) => setCategory(e.target.value)} className="h-9 rounded-md border border-border bg-background px-2 text-sm">
          <option value="all">All categories</option>
          {categories.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <button
          onClick={() => setFlaggedOnly((v) => !v)}
          className={`h-9 px-3 rounded-md border text-xs font-medium inline-flex items-center gap-1.5 ${flaggedOnly ? "border-red-300 bg-red-50 text-red-700" : "border-border text-muted-foreground hover:bg-muted"}`}
        >
          <AlertTriangle className="w-3.5 h-3.5" /> Out of range only
        </button>
      </div>

      {filtered.length === 0 ? (
        <p className="text-sm text-muted-foreground py-6 text-center">No analytes match these filters.</p>
      ) : view === "index" ? (
        <IndexView families={filtered} />
      ) : view === "charts" ? (
        <ChartsView families={filtered} />
      ) : (
        <MatrixView families={filtered} />
      )}
    </div>
  );
}
