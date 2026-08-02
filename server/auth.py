"""Session auth: one admin plus optional source-read-only provider logins.

First visit creates the admin login (Argon2id hash stored in SQLite);
afterwards /login authenticates and sets a signed session cookie. Legacy
PBKDF2 hashes still verify and are transparently re-hashed to Argon2 on the
next successful login. The SPA talks to /api/auth/me and redirects to /login
on 401.
"""

import base64
import hashlib
import json
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import DEMO_MODE, OWNER_EMAIL, env
from .db import get_setting, set_setting

RESET_REQUEST_TTL = 30 * 60  # seconds a reset code stays valid

router = APIRouter()

# Argon2id with the library's calibrated defaults (memory-hard, GPU-resistant).
_ph = PasswordHasher()


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    # New scheme: Argon2id ("$argon2id$...").
    if stored_hash.startswith("$argon2"):
        try:
            return _ph.verify(stored_hash, password)
        except (VerifyMismatchError, InvalidHashError, Exception):
            return False
    # Legacy scheme: PBKDF2-SHA256, kept so pre-Argon2 hashes still authenticate.
    try:
        scheme, iterations, salt_b64, hash_b64 = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return secrets.compare_digest(actual, expected)
    except Exception:
        return False


def make_password_hash(password: str) -> str:
    return _ph.hash(password)


def needs_rehash(stored_hash: str) -> bool:
    """True if the stored hash should be upgraded to current Argon2 params
    (covers both legacy PBKDF2 and outdated Argon2 parameters)."""
    if not stored_hash.startswith("$argon2"):
        return True
    try:
        return _ph.check_needs_rehash(stored_hash)
    except Exception:
        return False


def configured_password_hash() -> str:
    return env("APP_PASSWORD_HASH", "") or get_setting("admin_password_hash")


def setup_required() -> bool:
    if DEMO_MODE:
        return False
    return not configured_password_hash()


def is_logged_in(request: Request) -> bool:
    return DEMO_MODE or bool(request.session.get("logged_in"))


def require_login(request: Request) -> None:
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


def _auth_page(title: str, form_html: str, error: str = "") -> HTMLResponse:
    body = f"""
    <!doctype html>
    <html><head><title>{title}</title><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
      body{{font-family:Segoe UI,Arial,sans-serif;background:#f6f8fb;color:#172033;margin:0;display:grid;place-items:center;min-height:100vh}}
      form{{background:white;border:1px solid #d8e1ed;border-radius:12px;padding:28px;width:min(380px,90vw)}}
      h1{{font-size:22px;margin:0 0 6px}}
      input,button{{width:100%;padding:10px;margin-top:10px;border-radius:6px;border:1px solid #cbd5e1;box-sizing:border-box}}
      button{{background:#0f766e;color:white;font-weight:700;cursor:pointer;border:none}}
      .err{{color:#b91c1c;font-size:13px;min-height:1em}}
      p{{color:#475569;font-size:14px;line-height:1.45}}
      .brand{{display:flex;align-items:center;gap:10px;margin-bottom:8px}}
      .logo{{width:34px;height:34px;border-radius:8px;background:#0f766e;color:white;display:grid;place-items:center;font-weight:800;font-family:monospace}}
      details{{margin-top:14px;font-size:13px;color:#475569}}
      details summary{{cursor:pointer;color:#0f766e;font-weight:600}}
      code{{display:block;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:6px;padding:8px;margin-top:8px;font-size:11.5px;overflow-x:auto;white-space:pre}}
    </style></head><body>
      {form_html.replace('__ERROR__', error)}
    </body></html>
    """
    return HTMLResponse(body)


def login_page(error: str = "") -> HTMLResponse:
    return _auth_page(
        "Sign in — GlucoPilot",
        """
      <form method="post" action="/login">
        <div class="brand"><div class="logo">GP</div><h1>GlucoPilot</h1></div>
        <p class="err">__ERROR__</p>
        <input name="username" placeholder="Username" autocomplete="username">
        <input name="password" placeholder="Password" type="password" autocomplete="current-password">
        <button type="submit">Sign in</button>
        <details>
          <summary>Forgot your password?</summary>
          <p>This app is self-hosted: resets are done from the server terminal, so
          nothing sensitive ever appears in the browser.</p>
          <p><a href="/forgot" style="color:#0f766e;font-weight:600">Generate a reset code</a> —
          then paste it into a terminal on the server. The code is a one-time random
          token; it reveals nothing on its own and expires in 30 minutes.</p>
        </details>
      </form>
        """,
        error,
    )


def setup_page(error: str = "") -> HTMLResponse:
    return _auth_page(
        "Create admin login — GlucoPilot",
        """
      <form method="post" action="/setup">
        <div class="brand"><div class="logo">GP</div><h1>Create admin login</h1></div>
        <p>First-run setup. Only a PBKDF2 password hash is stored in the app database volume.</p>
        <p class="err">__ERROR__</p>
        <input name="username" value="admin" placeholder="Username" autocomplete="username">
        <input name="password" placeholder="Password" type="password" autocomplete="new-password">
        <input name="confirm" placeholder="Confirm password" type="password" autocomplete="new-password">
        <button type="submit">Create login</button>
      </form>
        """,
        error,
    )


@router.get("/forgot")
def forgot_password():
    """Issue a one-time reset code shown in the browser.

    The code itself is a random token — it contains and reveals nothing. Only
    its hash is stored; redeeming it requires shell access on the server
    (python -m server.reset_password --request <code>), so the new password is
    only ever printed in the server terminal.
    """
    code = secrets.token_urlsafe(24)
    set_setting(
        "reset_request",
        json.dumps({"hash": hashlib.sha256(code.encode()).hexdigest(), "ts": int(time.time())}),
    )
    return _auth_page(
        "Password reset — GlucoPilot",
        f"""
      <form onsubmit="return false">
        <div class="brand"><div class="logo">GP</div><h1>Password reset</h1></div>
        <p class="err">__ERROR__</p>
        <p>Paste this one-time code into a terminal on the server within 30 minutes:</p>
        <code>docker compose exec glucose-explorer \\
  python -m server.reset_password --request {code}</code>
        <p>The command prints your username and a new temporary password —
        in the terminal only, never in the browser.</p>
        <p><a href="/login" style="color:#0f766e;font-weight:600">Back to sign in</a></p>
      </form>
        """,
    )


@router.get("/login")
def login_form(request: Request):
    if setup_required():
        return setup_page()
    if is_logged_in(request):
        return RedirectResponse("/dashboard", status_code=303)
    return login_page()


@router.post("/setup")
def setup(request: Request, username: str = Form(...), password: str = Form(...), confirm: str = Form(...)):
    if not setup_required():
        return RedirectResponse("/login", status_code=303)
    username = username.strip() or "admin"
    if len(password) < 12:
        return setup_page("Use at least 12 characters.")
    if password != confirm:
        return setup_page("Passwords did not match.")
    set_setting("admin_username", username)
    set_setting("admin_password_hash", make_password_hash(password))
    request.session["logged_in"] = True
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if setup_required():
        return RedirectResponse("/login", status_code=303)
    expected_user = env("APP_USERNAME", "") or get_setting("admin_username") or "admin"
    expected_hash = configured_password_hash()
    if not expected_hash:
        return login_page("Admin login is not configured.")
    if secrets.compare_digest(username, expected_user) and verify_password(password, expected_hash):
        # Transparently upgrade a legacy/outdated DB-stored hash to Argon2.
        # (Skip when the hash comes from the APP_PASSWORD_HASH env override.)
        if not env("APP_PASSWORD_HASH", "") and needs_rehash(expected_hash):
            set_setting("admin_password_hash", make_password_hash(password))
        request.session["logged_in"] = True
        request.session["role"] = "admin"
        return RedirectResponse("/dashboard", status_code=303)

    # Provider logins — source data/settings remain read-only; P7 review events
    # use a separate attributable write surface.
    providers = load_providers()
    for i, p in enumerate(providers):
        if secrets.compare_digest(username, p.get("username", "")) and verify_password(password, p.get("password_hash", "")):
            if needs_rehash(p.get("password_hash", "")):
                providers[i]["password_hash"] = make_password_hash(password)
                save_providers(providers)
            request.session["logged_in"] = True
            request.session["role"] = "provider"
            request.session["provider_name"] = p.get("username")
            return RedirectResponse("/dashboard", status_code=303)

    return login_page("Invalid username or password.")


MAX_PROVIDERS = 4


def load_providers() -> list[dict]:
    raw = get_setting("providers")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [p for p in data if isinstance(p, dict) and p.get("username")] if isinstance(data, list) else []
    except ValueError:
        return []


def save_providers(providers: list[dict]) -> None:
    set_setting("providers", json.dumps(providers))


# --- Provider invites: a single-use link instead of a shared password. ---
# The admin generates a link and emails it; the provider picks their own
# username and password on a public page. Only the token's SHA-256 is stored,
# so a database read never yields a usable invite.

INVITE_TTL_SECONDS = 7 * 24 * 3600


def _invite_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_invites() -> list[dict]:
    raw = get_setting("provider_invites")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        invites = data if isinstance(data, list) else []
    except ValueError:
        return []
    now = int(time.time())
    return [
        invite for invite in invites
        if isinstance(invite, dict)
        and invite.get("token_hash")
        and int(invite.get("expires_at") or 0) > now
    ]


def save_invites(invites: list[dict]) -> None:
    set_setting("provider_invites", json.dumps(invites))


def _invite_public(invite: dict) -> dict:
    return {
        # The hash is not the token; exposing it to the admin UI as an id is safe.
        "id": invite["token_hash"][:12],
        "created_at": invite.get("created_at"),
        "expires_at": invite.get("expires_at"),
    }


def create_provider_invite() -> tuple[str, dict]:
    invites = load_invites()
    token = secrets.token_urlsafe(32)
    invite = {
        "token_hash": _invite_hash(token),
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + INVITE_TTL_SECONDS,
    }
    save_invites([*invites, invite])
    return token, invite


def _taken_usernames() -> set[str]:
    admin = env("APP_USERNAME", "") or get_setting("admin_username") or "admin"
    return {admin.lower(), *(p["username"].lower() for p in load_providers())}


def accept_provider_invite(token: str, username: str, password: str) -> None:
    """Consume an invite and create the provider login it authorizes."""
    username = username.strip()
    invites = load_invites()
    match = next((i for i in invites if secrets.compare_digest(i["token_hash"], _invite_hash(token))), None)
    if not match:
        raise HTTPException(status_code=410, detail="This invite link is invalid or has expired. Ask for a new one.")
    if not (2 <= len(username) <= 40):
        raise HTTPException(status_code=400, detail="Username must be 2-40 characters.")
    if username.lower() in _taken_usernames():
        raise HTTPException(status_code=400, detail="That username is taken — choose another.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    providers = load_providers()
    if len(providers) >= MAX_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Maximum of {MAX_PROVIDERS} provider logins reached.")
    # Consume the token first so a duplicate submit cannot create two logins.
    save_invites([i for i in invites if i["token_hash"] != match["token_hash"]])
    providers.append({"username": username, "password_hash": make_password_hash(password)})
    save_providers(providers)


@router.get("/api/provider/invites")
def provider_invites_list(request: Request):
    require_admin(request)
    return {"invites": [_invite_public(i) for i in load_invites()]}


@router.post("/api/provider/invites")
def provider_invites_create(request: Request):
    require_admin(request)
    if len(load_providers()) >= MAX_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Maximum of {MAX_PROVIDERS} provider logins reached.")
    token, invite = create_provider_invite()
    # The raw token appears exactly once, in this response; only its hash persists.
    return {"token": token, "invite": _invite_public(invite)}


@router.delete("/api/provider/invites/{invite_id}")
def provider_invites_revoke(invite_id: str, request: Request):
    require_admin(request)
    save_invites([i for i in load_invites() if i["token_hash"][:12] != invite_id])
    return {"invites": [_invite_public(i) for i in load_invites()]}


@router.get("/api/provider/invite/{token}")
def provider_invite_status(token: str):
    """Public: lets the invite page tell a live link from a dead one."""
    invites = load_invites()
    match = next((i for i in invites if secrets.compare_digest(i["token_hash"], _invite_hash(token))), None)
    if not match:
        return {"valid": False}
    return {"valid": True, "expires_at": match["expires_at"], "at_capacity": len(load_providers()) >= MAX_PROVIDERS}


@router.post("/api/provider/invite/accept")
async def provider_invite_accept(request: Request):
    body = await request.json()
    accept_provider_invite(
        str(body.get("token") or ""),
        str(body.get("username") or ""),
        str(body.get("password") or ""),
    )
    return {"ok": True}


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


def session_role(request: Request) -> str:
    if DEMO_MODE:
        return "admin"  # demo shows every page (incl. admin-only), on throwaway data
    # Sessions created before roles existed default to admin.
    return request.session.get("role", "admin") if is_logged_in(request) else "anonymous"


def require_admin(request: Request) -> None:
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    if session_role(request) != "admin":
        raise HTTPException(status_code=403, detail="Read-only provider access — this action is not permitted.")


def session_actor(request: Request) -> str:
    """Stable identity for rows a session may author: "owner" or "provider:<name>".

    Provider-writable surfaces (Companion threads, care notes) scope their rows
    by this value, so each provider sees their own material and nobody edits
    anyone else's.
    """
    if session_role(request) == "provider":
        return f"provider:{request.session.get('provider_name') or 'provider'}"
    return "owner"


def current_user(role: str = "admin", provider_name: str = "") -> dict:
    if role == "provider":
        return {
            "id": "provider",
            "email": OWNER_EMAIL,
            "full_name": provider_name or "Provider",
            "role": "provider",
        }
    profile = {}
    raw = get_setting("user_profile")
    if raw:
        try:
            profile = json.loads(raw)
        except ValueError:
            profile = {}
    username = "Demo User" if DEMO_MODE else (env("APP_USERNAME", "") or get_setting("admin_username") or "admin")
    return {
        "id": "owner",
        "email": OWNER_EMAIL,
        "full_name": username,
        "role": "admin",
        "demo": DEMO_MODE,
        **profile,
    }


@router.get("/api/auth/me")
def me(request: Request):
    require_login(request)
    return current_user(session_role(request), request.session.get("provider_name", ""))


@router.get("/api/provider/config")
def provider_config_get(request: Request):
    require_admin(request)
    return {
        "providers": [{"username": p["username"]} for p in load_providers()],
        "max": MAX_PROVIDERS,
    }


@router.post("/api/provider/config")
async def provider_config_set(request: Request):
    """Upsert or remove a read-only provider login (max 4). Body:
    {username, password} to add/update; {username, remove: true} to delete."""
    require_admin(request)
    body = await request.json()
    username = str(body.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Provider username is required.")

    providers = load_providers()
    existing = next((p for p in providers if p["username"] == username), None)

    if body.get("remove"):
        providers = [p for p in providers if p["username"] != username]
        save_providers(providers)
        return provider_config_get(request)

    password = body.get("password") or ""
    if existing is None:
        if len(providers) >= MAX_PROVIDERS:
            raise HTTPException(status_code=400, detail=f"Maximum of {MAX_PROVIDERS} provider logins.")
        if len(password) < 8:
            raise HTTPException(status_code=400, detail="Provider password must be at least 8 characters.")
        providers.append({"username": username, "password_hash": make_password_hash(password)})
    elif password:
        if len(password) < 8:
            raise HTTPException(status_code=400, detail="Provider password must be at least 8 characters.")
        existing["password_hash"] = make_password_hash(password)
    save_providers(providers)
    return provider_config_get(request)


@router.post("/api/auth/me")
async def update_me(request: Request):
    require_admin(request)
    patch = await request.json()
    raw = get_setting("user_profile")
    profile = {}
    if raw:
        try:
            profile = json.loads(raw)
        except ValueError:
            profile = {}
    profile.update({k: v for k, v in patch.items() if k not in ("id", "email", "role")})
    set_setting("user_profile", json.dumps(profile))
    return current_user()


@router.post("/api/auth/logout")
def api_logout(request: Request):
    request.session.clear()
    return {"ok": True}
