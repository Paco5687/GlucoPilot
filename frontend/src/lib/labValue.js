// A result the lab printed as "<55" is below its reporting limit, not 55. The
// row stores value 55 and keeps the printed "<55" in original_value (the
// server's lab_audit.censored reads it the same way).
const CENSOR_RE = /^\s*(<=|>=|=<|=>|≤|≥|<|>)\s*-?\d/;
const CENSOR_SIGN = { "<": "<", "<=": "≤", "=<": "≤", "≤": "≤", ">": ">", ">=": "≥", "=>": "≥", "≥": "≥" };

// "<", "≤", ">" or "≥" for a below/above-limit result; "" otherwise.
export function censorSign(lab) {
  const match = CENSOR_RE.exec(String(lab?.original_value ?? lab?.value_text ?? ""));
  return match ? CENSOR_SIGN[match[1]] : "";
}

// The result as reported: "<55" for a below-limit result, else the value.
export function valueText(lab) {
  return `${censorSign(lab)}${lab?.value ?? ""}`;
}

// A row whose source printed a status such as "IN PROGRESS" where an older
// extraction stored 0: not a result, so it has no place in a trend.
export function isNotAResult(lab) {
  return Array.isArray(lab?.validation_issues) && lab.validation_issues.some((issue) => issue?.code === "not_a_result");
}
