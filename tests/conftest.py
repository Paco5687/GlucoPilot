"""Test setup: isolate DATA_DIR and disable demo/sync before importing the app."""

import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="glucopilot-test-"))
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
os.environ["DEMO_MODE"] = "false"
os.environ["SYNC_ENABLED"] = "false"

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    from server.main import app

    with TestClient(app) as c:  # runs lifespan (init_db)
        yield c
