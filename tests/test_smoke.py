"""End-to-end smoke test: the app boots, auth works, entities + provider round-trip."""

OWNER = "owner@glucopilot.local"


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_first_run_setup_and_me(client):
    r = client.post(
        "/setup",
        data={"username": "admin", "password": "testpassword123", "confirm": "testpassword123"},
        follow_redirects=False,
    )
    assert r.status_code in (303, 200)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["role"] == "admin"


def test_entity_crud(client):
    created = client.post(
        "/api/entities/GlucoseReading",
        json={"value": 120, "timestamp": "2026-07-18T12:00:00Z", "source": "test", "owner_email": OWNER},
    )
    assert created.status_code == 200
    rows = client.post("/api/entities/GlucoseReading/query", json={"limit": 5}).json()
    assert any(row["value"] == 120 for row in rows)


def test_operator_filter(client):
    client.post(
        "/api/entities/GlucoseReading",
        json={"value": 200, "timestamp": "2026-07-18T13:00:00Z", "source": "test", "owner_email": OWNER},
    )
    rows = client.post(
        "/api/entities/GlucoseReading/query",
        json={"filter": {"value": {"$gte": 150}}, "limit": 10},
    ).json()
    assert rows and all(r["value"] >= 150 for r in rows)


def test_provider_config_roundtrip(client):
    client.post("/api/provider/config", json={"username": "drtest", "password": "providerpw123"})
    cfg = client.get("/api/provider/config").json()
    assert cfg["max"] == 4
    assert any(p["username"] == "drtest" for p in cfg["providers"])


def test_unknown_entity_rejected(client):
    assert client.post("/api/entities/NotAThing/query", json={}).status_code == 404


def test_bug_report_fallback(client):
    # No GitHub token configured → returns a pre-filled new-issue URL.
    r = client.post("/api/bug-report", json={"description": "test bug", "context": {"page": "/dashboard"}})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False and "/issues/new" in data["fallback_url"]
    assert client.post("/api/bug-report", json={"description": " "}).status_code == 400
