-- SYNTHETIC FIXTURE ONLY: schema used before the ordered migration registry.
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

INSERT INTO entities (id, type, data, created_date, updated_date)
VALUES (
    'synthetic-pre-registry-row',
    'GlucoseReading',
    '{"owner_email":"owner@glucopilot.local","timestamp":"2026-01-01T00:00:00Z","value":123,"source":"synthetic-fixture"}',
    '2026-01-01T00:00:01Z',
    '2026-01-01T00:00:01Z'
);
