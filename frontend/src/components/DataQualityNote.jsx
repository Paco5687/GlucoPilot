export default function DataQualityNote({ label, quality }) {
  if (!quality) return null;
  const warning = !quality.ai_eligible || quality.reliability === "low";
  return (
    <div className={`report-card rounded-lg border px-3 py-2 text-[11px] ${warning ? "border-amber-300 bg-amber-50 text-amber-800" : "border-border bg-muted/30 text-muted-foreground"}`}>
      <span className="font-semibold">{label} data quality:</span>{" "}
      {quality.coverage_pct}% coverage · {quality.reliability} reliability
      {quality.data_through ? ` · data through ${quality.data_through}` : " · no data-through date"}.
      {quality.exclusion_reasons?.length > 0 && ` Excluded from AI summary: ${quality.exclusion_reasons.join("; ")}.`}
    </div>
  );
}
