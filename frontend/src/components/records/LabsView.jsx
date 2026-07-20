import { useMemo, useState } from "react";
import { Input } from "@/components/ui/input";
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, ReferenceArea, CartesianGrid,
} from "recharts";
import { FlaskConical, AlertTriangle, Search, List, LineChart as LineIcon, Grid3x3, ChevronDown, ChevronRight } from "lucide-react";

const ABNORMAL = new Set(["high", "low", "critical", "abnormal"]);

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

const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const rawTokens = (name) => (name || "").toLowerCase().replace(/[.,()/\-]/g, " ").split(/\s+/).filter(Boolean);

// Merge only pure formatting/word-order variants; keep clinically-distinct
// qualifiers (free/total/specimen) so different measurements never collapse.
const CANON_STOP = new Set(["", "reflex", "w", "with"]);
function canonKey(name) {
  return rawTokens(name).filter((t) => !CANON_STOP.has(t)).sort().join(" ");
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

function useFamilies(labs) {
  return useMemo(() => {
    // 1. bucket into variants keyed by (canonical name + unit)
    const byVariant = new Map();
    for (const lab of labs) {
      if (lab.value == null || !lab.test_name) continue;
      const key = `${canonKey(lab.test_name)}|${unitKey(lab.unit)}`;
      if (!byVariant.has(key)) byVariant.set(key, []);
      byVariant.get(key).push(lab);
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
  }, [labs]);
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
  const families = useFamilies(labs);
  const [view, setView] = useState("index");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("all");
  const [flaggedOnly, setFlaggedOnly] = useState(false);

  const categories = useMemo(
    () => [...new Set(families.flatMap((f) => f.variants.map((v) => v.category)))].sort(),
    [families]
  );

  const filtered = useMemo(() => {
    const q = query.toLowerCase();
    return families
      .map((f) => {
        const variants = f.variants.filter((v) =>
          (!q || f.label.toLowerCase().includes(q) || v.name.toLowerCase().includes(q)) &&
          (category === "all" || v.category === category) &&
          (!flaggedOnly || v.abnormal)
        );
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

      {/* Filters */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative flex-1 min-w-[180px]">
          <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search analytes…" className="pl-8 h-9 text-sm" />
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
