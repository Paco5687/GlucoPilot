export const ANALYTICS_CONFIDENCE_VERSION = "analytics-confidence/1.0.0";

const Z_95 = 1.959963984540054;
const LANGUAGE_LEADS = {
  exploratory: "An exploratory signal was observed in this limited sample.",
  emerging: "An emerging observational association was detected.",
  reproduced: "The association repeated in a later temporal holdout.",
  "not-reproduced": "An initial signal was not reproduced in the later temporal holdout.",
  invalid: "The available data could not support a valid estimate.",
};
const PROHIBITED_CLAIMS = ["causes", "clinically confirmed", "definitive", "proves"];

export function pearsonCorrelation(pairs) {
  if (pairs.length < 4) return null;
  const n = pairs.length;
  const sumX = pairs.reduce((sum, pair) => sum + pair.x, 0);
  const sumY = pairs.reduce((sum, pair) => sum + pair.y, 0);
  const sumXY = pairs.reduce((sum, pair) => sum + pair.x * pair.y, 0);
  const sumX2 = pairs.reduce((sum, pair) => sum + pair.x * pair.x, 0);
  const sumY2 = pairs.reduce((sum, pair) => sum + pair.y * pair.y, 0);
  const numerator = n * sumXY - sumX * sumY;
  const denominator = Math.sqrt(
    (n * sumX2 - sumX * sumX) * (n * sumY2 - sumY * sumY),
  );
  if (!Number.isFinite(denominator) || denominator === 0) return null;
  return Math.max(-1, Math.min(1, numerator / denominator));
}

function rounded(value) {
  return value == null ? null : Number(value.toFixed(4));
}

function interval(r, count) {
  if (r == null || count <= 3) return null;
  if (Math.abs(r) === 1) return [r, r];
  const transformed = Math.atanh(r);
  const margin = Z_95 / Math.sqrt(count - 3);
  return [Math.tanh(transformed - margin), Math.tanh(transformed + margin)];
}

export function correlationConfidence(pairs, expectedDays = null) {
  const ordered = [...pairs]
    .filter((pair) => Number.isFinite(pair.x) && Number.isFinite(pair.y))
    .sort((left, right) => String(left.day || "").localeCompare(String(right.day || "")));
  const r = pearsonCorrelation(ordered);
  const validDays = new Set(ordered.map((pair, index) => pair.day || String(index))).size;
  let expected = expectedDays;
  if (expected == null && ordered.length && ordered.every((pair) => pair.day)) {
    const first = new Date(`${ordered[0].day}T00:00:00Z`).getTime();
    const last = new Date(`${ordered[ordered.length - 1].day}T00:00:00Z`).getTime();
    expected = Math.round((last - first) / 86400000) + 1;
  }
  if (!Number.isFinite(expected) || expected < 0) expected = validDays;
  const ci = interval(r, ordered.length);
  let discoveryStatus = "invalid";
  let replication = {
    attempted: false,
    kind: null,
    discovery_sample_count: ordered.length,
    replication_sample_count: 0,
    discovery_effect: rounded(r),
    replication_effect: null,
    status: "not-attempted",
  };

  if (r != null && (validDays <= 7 || ordered.length < 14)) {
    discoveryStatus = "exploratory";
  } else if (r != null && ordered.length < 28) {
    discoveryStatus = "emerging";
  } else if (r != null) {
    const midpoint = Math.floor(ordered.length / 2);
    const discovery = pearsonCorrelation(ordered.slice(0, midpoint));
    const holdout = pearsonCorrelation(ordered.slice(midpoint));
    const reproduced = discovery != null && holdout != null
      && Math.abs(discovery) >= 0.3 && Math.abs(holdout) >= 0.3
      && Math.sign(discovery) === Math.sign(holdout);
    discoveryStatus = reproduced ? "reproduced" : "not-reproduced";
    replication = {
      attempted: true,
      kind: "temporal_holdout",
      discovery_sample_count: midpoint,
      replication_sample_count: ordered.length - midpoint,
      discovery_effect: rounded(discovery),
      replication_effect: rounded(holdout),
      status: discoveryStatus,
    };
  }

  const missingDays = Math.max(0, expected - validDays);
  const precision = ci ? Math.max(0, 1 - (ci[1] - ci[0]) / 2) : 0;
  const coverage = expected ? Math.min(1, validDays / expected) : 0;
  const replicationBonus = discoveryStatus === "reproduced" ? 0.1 : 0;
  const replicationPenalty = discoveryStatus === "not-reproduced" ? 0.2 : 0;
  const confidenceScore = discoveryStatus === "invalid" ? 0 : Number(Math.max(
    0,
    Math.min(1, 0.35 * coverage + 0.35 * Math.min(1, ordered.length / 30)
      + 0.3 * precision + replicationBonus - replicationPenalty),
  ).toFixed(3));
  return {
    version: ANALYTICS_CONFIDENCE_VERSION,
    sample_count: ordered.length,
    valid_days: validDays,
    effect_size: {
      metric: "pearson_r",
      value: rounded(r),
      magnitude: r == null ? "unknown" : Math.abs(r) >= 0.5 ? "large" : Math.abs(r) >= 0.3 ? "moderate" : Math.abs(r) >= 0.1 ? "small" : "negligible",
      direction: r > 0 ? "positive" : r < 0 ? "negative" : "none",
    },
    confidence_interval: ci ? {
      level: 0.95,
      lower: rounded(ci[0]),
      upper: rounded(ci[1]),
      metric: "pearson_r",
      method: "fisher_z",
    } : null,
    missingness: {
      expected_days: expected,
      valid_days: validDays,
      missing_days: missingDays,
      missing_rate: expected ? rounded(missingDays / expected) : null,
    },
    temporal_direction: "same-day contemporaneous association",
    discovery_status: discoveryStatus,
    replication,
    confidence_score: confidenceScore,
    confidence_label: confidenceScore >= 0.85 ? "high" : confidenceScore >= 0.6 ? "medium" : "low",
    language: {
      strength: discoveryStatus,
      lead: LANGUAGE_LEADS[discoveryStatus],
      definitive_allowed: false,
      causal_allowed: false,
      prohibited_claims: PROHIBITED_CLAIMS,
    },
  };
}
