-- SYNTHETIC FIXTURE ONLY: schema after migration 1 and before migration 2.
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0)
);

CREATE TABLE app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_date TEXT NOT NULL,
    updated_date TEXT NOT NULL
);

CREATE INDEX idx_entities_type ON entities(type);
CREATE INDEX idx_entities_type_ts
    ON entities(type, json_extract(data, '$.timestamp'));
CREATE INDEX idx_entities_type_date
    ON entities(type, json_extract(data, '$.date'));

INSERT INTO schema_migrations (version, name, checksum, applied_at, duration_ms)
VALUES (
    1,
    'legacy_json_store_baseline',
    '{{MIGRATION_1_CHECKSUM}}',
    '2026-01-01T00:00:00Z',
    0
);

INSERT INTO entities (id, type, data, created_date, updated_date)
VALUES (
    'synthetic-tracked-baseline-row',
    'GlucoseReading',
    '{"owner_email":"owner@glucopilot.local","timestamp":"2026-01-02T00:00:00Z","value":124,"source":"synthetic-fixture"}',
    '2026-01-02T00:00:01Z',
    '2026-01-02T00:00:01Z'
);
