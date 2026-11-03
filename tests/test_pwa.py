"""The installable-app files: manifest and service worker are served as
themselves (not swallowed by the SPA fallback), and the manifest meets what
Android needs to open GlucoPilot in its own window."""

import json
from pathlib import Path

PUBLIC = Path(__file__).resolve().parent.parent / "frontend" / "public"


def test_manifest_and_service_worker_served_from_dist(client, monkeypatch):
    monkeypatch.setattr("server.main.FRONTEND_DIST", PUBLIC)

    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert manifest.json()["name"] == "GlucoPilot"

    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert worker.headers["content-type"].startswith("text/javascript")
    assert worker.headers["cache-control"] == "no-cache"
    assert "addEventListener('fetch'" in worker.text


def test_missing_build_is_a_404_not_the_spa(client, monkeypatch, tmp_path):
    monkeypatch.setattr("server.main.FRONTEND_DIST", tmp_path)
    assert client.get("/manifest.webmanifest").status_code == 404
    assert client.get("/sw.js").status_code == 404


def test_manifest_is_installable_as_standalone_app():
    manifest = json.loads((PUBLIC / "manifest.webmanifest").read_text())
    assert manifest["display"] == "standalone"
    assert manifest["start_url"].startswith("/")
    assert manifest["scope"] == "/"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(icon["purpose"] == "maskable" for icon in manifest["icons"])
    for icon in manifest["icons"]:
        assert (PUBLIC / icon["src"].lstrip("/")).is_file(), icon["src"]
