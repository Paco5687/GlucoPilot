# Data quality envelopes

GlucoPilot attaches a deterministic, versioned quality envelope to derived CGM, pump-TDD, wearable, nutrition, and cycle results. The envelope makes missingness and freshness visible to the UI and to AI consumers instead of allowing a sparse value to look complete.

The `data-quality/1.0.0` envelope contains:

- domain, observed and expected units, and coverage percentage;
- data-through date and freshness in days;
- reliability score and `high`, `medium`, or `low` label;
- limitations and explicit AI-exclusion reasons;
- `complete` and `ai_eligible` decisions; and
- a deterministic input-data hash.

## Operational thresholds

| Domain | Minimum coverage for AI | Maximum age | Coverage unit |
| --- | ---: | ---: | --- |
| CGM | 70% | 2 days | 5-minute readings |
| Pump TDD | 50% | 14 days | complete daily totals |
| Insulin response | 100% | 14 days | 8 clean correction boluses |
| Wearables | 50% | 7 days | days with a relevant metric |
| Nutrition | 30% | 7 days | days with positive carbohydrate logs |
| Cycle | 50% | 45 days | days with a recorded or inferred phase |

These domain floors are combined with a minimum composite reliability score of `0.60`. Data must pass the coverage floor, freshness limit, composite score, and any domain-specific blockers before it may enter an AI prompt. These are conservative product thresholds, not clinical targets, and they do not assess the person's health. A value below a threshold remains available to deterministic UI code with its warning envelope, but is excluded from AI summaries and conclusions by default.

Pump-reported and calculated TDD remain separate. A pump day is complete only when it has a usable pump-reported total or a calculated total backed by complete delivered-basal coverage. Programmed basal is not treated as delivery. Nutrition coverage describes logging behavior only; a day without a log does not mean zero carbohydrate intake.

Calculators are pure and live in `server/data_quality.py`. Golden synthetic fixtures lock the thresholds, missing-data behavior, duplicate handling, and AI-exclusion contract.
