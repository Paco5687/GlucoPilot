import { describe, expect, it } from "vitest";
import {
  ANALYTICS_CONFIDENCE_VERSION,
  correlationConfidence,
} from "./analyticsConfidence";

function linearPairs(days) {
  return Array.from({ length: days }, (_, index) => ({
    day: `2026-01-${String(index + 1).padStart(2, "0")}`,
    x: index + 1,
    y: (index + 1) * 2,
  }));
}

describe("analytics confidence browser parity", () => {
  it("labels even a perfect seven-day correlation exploratory", () => {
    const result = correlationConfidence(linearPairs(7), 7);

    expect(result.version).toBe(ANALYTICS_CONFIDENCE_VERSION);
    expect(result.sample_count).toBe(7);
    expect(result.effect_size.value).toBe(1);
    expect(result.confidence_interval).toMatchObject({ lower: 1, upper: 1 });
    expect(result.discovery_status).toBe("exploratory");
    expect(result.confidence_score).toBe(0.732);
    expect(result.confidence_label).toBe("medium");
    expect(result.language.definitive_allowed).toBe(false);
  });

  it("requires a same-direction temporal holdout before reproduced", () => {
    const reproduced = correlationConfidence(linearPairs(28), 28);
    const reversed = linearPairs(28).map((pair, index) => ({
      ...pair,
      y: index < 14 ? index + 1 : -(index + 1),
    }));
    const notReproduced = correlationConfidence(reversed, 28);

    expect(reproduced.discovery_status).toBe("reproduced");
    expect(reproduced.confidence_score).toBe(1);
    expect(reproduced.replication.status).toBe("reproduced");
    expect(notReproduced.discovery_status).toBe("not-reproduced");
    expect(notReproduced.replication.status).toBe("not-reproduced");
  });
});
