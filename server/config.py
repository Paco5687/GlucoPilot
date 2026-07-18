import os
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent
FRONTEND_DIST = Path(env("FRONTEND_DIST", str(REPO_DIR / "frontend" / "dist")))
DATA_DIR = Path(env("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "app.sqlite3"

# Single-user app: every record is owned by this synthetic identity. The
# frontend's me().email and all server-side sync writes use the same value so
# owner_email filters always match.
OWNER_EMAIL = env("APP_OWNER_EMAIL", "owner@glucopilot.local")

APP_TIMEZONE = env("APP_TIMEZONE", "America/New_York")

# Demo mode: no login required (auto read-only-ish admin session), seeds
# synthetic data on first start. NEVER enable on an instance holding real data.
DEMO_MODE = env_bool("DEMO_MODE", False)

DEXCOM_BASE_URLS = {
    "production_us": "https://api.dexcom.com",
    "production_eu": "https://api.dexcom.eu",
    "production_jp": "https://api.dexcom.jp",
    # NOTE: sandbox is intentionally NOT wired into DEXCOM_ENV validation
    # anywhere else in the app. Only the real production account may be used —
    # the sandbox flow would consume the only Dexcom user-license slot.
}


def dexcom_base_url() -> str:
    return DEXCOM_BASE_URLS.get(env("DEXCOM_ENV", "production_us"), DEXCOM_BASE_URLS["production_us"])


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-sonnet-5")
