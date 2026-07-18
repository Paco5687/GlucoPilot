import { useMemo, useState } from "react";
import { Input } from "@/components/ui/input";
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, ReferenceArea, CartesianGrid,
} from "recharts";
import { FlaskConical, AlertTriangle, Search, List, LineChart as LineIcon, Grid3x3, ChevronDown, ChevronRight } from "lucide-react";

const ABNORMAL = new Set(["high", "low", "critical", "abnormal"]);

// --- data prep -------------------------------------------------------------

function isAbnormal(flag, value, lo, hi) {
  const f = (flag || "").toLowerCase();
  if (ABNORMAL.has(f)) return f;
  if (value != null && lo != null && value < lo) return "low";
  if (value != null && hi != null && value > hi) return "high";
  return "";
}

function useAnalytes(labs) {
  return useMemo(() => {
    const byTest = new Map();
    for (const lab of labs) {
      if (lab.value == null || !lab.test_name) continue;
      if (!byTest.has(lab.test_name)) byTest.set(lab.test_name, []);
      byTest.get(lab.test_name).push(lab);
    }
    const analytes = [];
    for (const [name, points] of byTest) {
      points.sort((a, b) => String(a.collected_date).localeCompare(String(b.collected_date)));
      const latest = points[points.length - 1];
      const refLow = points.find((p) => p.reference_low != null)?.reference_low ?? null;
      const refHigh = points.find((p) => p.reference_high != null)?.reference_high ?? null;
      const abn = isAbnormal(latest.flag, latest.value, refLow, refHigh);
      analytes.push({
        name,
        category: points[points.length - 1].category || "Other",
        points, latest, refLow, refHigh,
        unit: latest.unit || "",
        abnormal: abn,
        count: points.length,
        lastDate: latest.collected_date || "",
      });
    }
    // abnormal first, then most recent, then name
    analytes.sort((a, b) =>
      (b.abnormal ? 1 : 0) - (a.abnormal ? 1 : 0) ||
      String(b.lastDate).localeCompare(String(a.lastDate)) ||
      a.name.localeCompare(b.name)
    );
    return analytes;
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

// --- views -----------------------------------------------------------------

function IndexView({ analytes }) {
  const [expanded, setExpanded] = useState(null);
  // group by category, preserving abnormal-first order within each
  const groups = useMemo(() => {
    const m = new Map();
    for (const a of analytes) { if (!m.has(a.category)) m.set(a.category, []); m.get(a.category).push(a); }
    return [...m.entries()].sort((x, y) => x[0].localeCompare(y[0]));
  }, [analytes]);
  const [collapsed, setCollapsed] = useState(() => new Set());

  return (
    <div className="space-y-4">
      {groups.map(([cat, items]) => {
        const isCol = collapsed.has(cat);
        const abn = items.filter((a) => a.abnormal).length;
        return (
          <div key={cat} className="bg-card rounded-xl border border-border overflow-hidden">
            <button
              onClick={() => setCollapsed((p) => { const n = new Set(p); n.has(cat) ? n.delete(cat) : n.add(cat); return n; })}
              className="w-full flex items-center justify-between px-4 py-2.5 hover:bg-accent/40"
            >
              <span className="flex items-center gap-2 text-sm font-semibold">
                {isCol ? <ChevronRight className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                {cat}
                <span className="text-xs font-normal text-muted-foreground">{items.length}</span>
              </span>
              {abn > 0 && <span className="text-[10px] px-2 py-0.5 rounded-full bg-red-100 text-red-700 font-medium">{abn} out of range</span>}
            </button>
            {!isCol && (
              <div className="divide-y divide-border">
                {items.map((a) => (
                  <div key={a.name}>
                    <button onClick={() => setExpanded(expanded === a.name ? null : a.name)} className="w-full flex items-center gap-3 px-4 py-2 hover:bg-accent/30 text-left">
                      <span className="flex-1 min-w-0 text-sm truncate">{a.name}</span>
                      <Sparkline points={a.points} color={a.abnormal ? "#dc2626" : "hsl(var(--primary))"} />
                      <span className="w-24 text-right text-sm font-medium tabular-nums">
                        {a.latest.value}<span className="text-[10px] text-muted-foreground ml-0.5">{a.unit}</span>
                      </span>
                      <span className="w-24 text-right text-[11px] text-muted-foreground tabular-nums hidden sm:block">
                        {a.refLow != null && a.refHigh != null ? `${a.refLow}–${a.refHigh}` : "—"}
                      </span>
                      <span className="w-16 flex justify-end">{flagBadge(a.abnormal)}</span>
                    </button>
                    {expanded === a.name && <div className="px-4 pb-3 bg-accent/10"><LabTrend a={a} /></div>}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function ChartsView({ analytes }) {
  return (
    <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-4">
      {analytes.map((a) => (
        <div key={a.name} className="bg-card rounded-xl border border-border p-4">
          <div className="flex items-start justify-between gap-2 mb-1">
            <div>
              <h4 className="font-semibold text-sm">{a.name}</h4>
              <p className="text-xs text-muted-foreground">
                {a.count} result{a.count === 1 ? "" : "s"}{a.refLow != null && a.refHigh != null ? ` · ref ${a.refLow}–${a.refHigh} ${a.unit}` : ""}
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

function MatrixView({ analytes }) {
  // Month-bucketed heatmap: analytes (rows) × months (cols), cell = latest that month.
  const { months, rows } = useMemo(() => {
    const monthSet = new Set();
    const rows = analytes.map((a) => {
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
  }, [analytes]);

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
          {rows.map(({ a, cells }) => (
            <tr key={a.name}>
              <td className="sticky left-0 bg-card z-10 text-xs pr-2 whitespace-nowrap max-w-[160px] truncate" title={a.name}>{a.name}</td>
              {months.map((m) => {
                const c = cells.get(m);
                return (
                  <td key={m} className="p-0">
                    {c ? (
                      <div title={`${a.name}: ${c.value} ${a.unit} (${c.date})${c.abn ? " — " + c.abn : ""}`}
                        className="w-4 h-4 rounded-sm mx-auto" style={{ backgroundColor: color(c.abn) }} />
                    ) : <div className="w-4 h-4 mx-auto rounded-sm bg-muted/40" />}
                  </td>
                );
              })}
            </tr>
          ))}
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
  const analytes = useAnalytes(labs);
  const [view, setView] = useState("index");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("all");
  const [flaggedOnly, setFlaggedOnly] = useState(false);

  const categories = useMemo(
    () => [...new Set(analytes.map((a) => a.category))].sort(),
    [analytes]
  );
  const filtered = useMemo(() => analytes.filter((a) =>
    (!query || a.name.toLowerCase().includes(query.toLowerCase())) &&
    (category === "all" || a.category === category) &&
    (!flaggedOnly || a.abnormal)
  ), [analytes, query, category, flaggedOnly]);

  const abnormalCount = analytes.filter((a) => a.abnormal).length;
  if (analytes.length === 0) return null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h2 className="font-semibold text-base flex items-center gap-2">
          <FlaskConical className="w-4 h-4 text-primary" /> Lab trends
          <span className="text-xs font-normal text-muted-foreground">{analytes.length} analytes</span>
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
        <IndexView analytes={filtered} />
      ) : view === "charts" ? (
        <ChartsView analytes={filtered} />
      ) : (
        <MatrixView analytes={filtered} />
      )}
    </div>
  );
}
