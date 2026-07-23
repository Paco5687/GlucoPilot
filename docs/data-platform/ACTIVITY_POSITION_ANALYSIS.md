# Activity and position analysis

Status: active observational contract

Implementations: `server/activity_position.py`,
`frontend/src/components/wearables/ActivityPositionAnalysis.jsx`

Contract versions: `activity-position-interval/1.0.0`,
`activity-position-analysis/1.0.0`

P4 adds timestamped activity and position intervals without treating daily
wearable totals as event-time evidence. Manual intervals can record resting or
walking activity and sitting, standing, lying, or upright position. Google
Health may add a low-confidence walking interval only when a short, explicitly
timestamped step interval is available. A daily step count, active-minute
total, heart-rate sample, or sedentary label does not establish body position.

## Immutable corrections and precedence

`activity_position_intervals` and `activity_position_events` are append-only.
A correction creates a new manual row linked through `correction_of_id`; it
does not update or delete the original row. At event time:

1. a manual interval takes precedence over an overlapping wearable inference;
2. a later manual correction takes precedence over the row it corrects; and
3. every overridden row remains visible with the ID of its override.

The source interval, origin, input hash, model version, confidence,
limitations, actor, reason, before state, and after state survive backup and
restore verification.

## Observational calculations

The builder joins effective timestamped intervals to four calculation families:

- glucose slope within the interval;
- glucose slope between 04:00 and 12:00 local time within the interval;
- clean P3 bolus-response events; and
- fixed P2 CGM-minus-fingerstick comparisons.

Each activity and position stratum reports the observed mean, unit, sample
count, total and measured interval counts, interval missingness, a 95%
confidence interval when calculable, expected/valid/missing days, discovery
status, replication status, numerical confidence, algorithm version, input
hash, and exact source links. `not-attempted` is an explicit replication
outcome; the implementation does not claim reproduction where the shared
framework has no eligible holdout.

All results are temporal associations. They prohibit causal and definitive
language and do not recommend treatment or activity changes.

## Evidence consumers

Evidence Bundle 2.3 exposes source-linked `ActivityPositionEffect` items for
bounded wearable/analytics queries. The Wearables page and Visit Report show
all available effects with sample, missingness, confidence, and replication
context. Companion receives an effect only when it has at least 14 samples, an
`emerging` or `reproduced` discovery status, and a numerical confidence score
of at least 0.50. Exploratory, invalid, and otherwise non-qualifying effects
remain available to the user but are omitted from Companion grounding.

## Rollback

Migration 19 is additive. Rolling application code back leaves the immutable
interval and event tables untouched. No generic JSON rows are removed, and no
second operational database is introduced.
