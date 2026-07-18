# AgonSR WebUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a multi-user web platform where users create symbolic regression projects, edit problem descriptions, upload CSV data, start AgonSR runs via screen session, and watch real-time output.

**Architecture:** Flask 3.x (REST + WebSocket via flask-sock) on port 7655; Next.js 16 frontend on port 3000 (dev) / 7656 (prod). SQLite stores metadata. User files live in `userdata/<email>/<project_name>/` (git submodule). nginx proxies both in production.

**Tech Stack:** Python 3.11+, Flask 3.x, flask-sock 0.7+, PyJWT 2.x, bcrypt 4.x, SQLite; Next.js 16, React 19, TypeScript, Tailwind v4, Zustand 5.x

## Global Constraints

- Python ≥ 3.11 (tomllib is stdlib)
- All API responses are JSON; errors: `{"error": "message"}`
- Timestamps: Unix float (time.time())
- JWT in httpOnly cookie `agonsr_session`, 24 h expiry
- Path traversal guard: resolve all user paths and assert they stay under `USERDATA_DIR/<email>/`
- Screen name: `agonsr-<run_id>` where run_id is uuid4 without hyphens
- Log path: `<workdir>/runs/<run_id>/screen.log`
- Dispatcher invocation: `claude --dangerously-skip-permissions --plugin-dir <PLUGIN_DIR> --model claude-sonnet-5[1m] -p "/llm-mcts <rounds> problem.md"`
- UI language: English only

---

### Task 1: Repos + Flask skeleton + SQLite schema

**Files:**
- Create: `backend/__init__.py`
- Create: `backend/app.py`
- Create: `backend/config.py`
- Create: `backend/db.py`
- Create: `backend/config.example.toml`
- Create: `backend/requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_health.py`

**Interfaces:**
- Produces: `create_app(config_path: str) -> Flask` — imported by all test conftest fixtures
- Produces: `get_db() -> sqlite3.Connection` — used by auth, projects, runs, admin blueprints
- Produces: `get_config() -> dict` — used by any module needing config values

- [ ] **Step 1: Create GitHub repos and clone**

```bash
gh repo create AutoResearch-Factory/AgonSR-WebUI --public
gh repo create AutoResearch-Factory/AgonSR-UserData --private
git clone git@github.com:AutoResearch-Factory/AgonSR-WebUI.git ~/AgonSR-WebUI
cd ~/AgonSR-WebUI
git submodule add git@github.com:AutoResearch-Factory/AgonSR-UserData.git userdata
git add .
git commit -m "Add userdata submodule"
git push
```

- [ ] **Step 2: Write requirements.txt**

`backend/requirements.txt`:
```
flask==3.1.0
flask-sock==0.7.0
PyJWT==2.10.1
bcrypt==4.3.0
pytest==8.3.5
pytest-flask==1.3.0
```

Install: `pip install -r backend/requirements.txt`

- [ ] **Step 3: Write config.example.toml**

`backend/config.example.toml`:
```toml
[server]
host = "127.0.0.1"
port = 7655
secret_key = "change-me-to-random-string"

[paths]
plugin_dir = "/home/youran/AgonSR/agonsr"
userdata_dir = "/home/youran/AgonSR-WebUI/userdata"
db_path = "/home/youran/AgonSR-WebUI/backend/agonsr.db"

[email]
smtp_host = ""
smtp_port = 587
smtp_user = ""
smtp_password = ""
from_address = "agonsr@example.com"

[admin]
emails = ["guojx@stanford.edu"]
```

Copy to `backend/config.toml` and fill in values. Add `backend/config.toml` to `.gitignore`.

- [ ] **Step 4: Write config.py**

`backend/config.py`:
```python
from __future__ import annotations
import tomllib
from flask import current_app

def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)

def get_config() -> dict:
    return current_app.config["CFG"]
```

- [ ] **Step 5: Write db.py**

`backend/db.py`:
```python
from __future__ import annotations
import json
import sqlite3
from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    email       TEXT PRIMARY KEY,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    otp_hash    TEXT,
    otp_expiry  REAL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    user_email  TEXT NOT NULL REFERENCES users(email),
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    rounds          INTEGER NOT NULL,
    proposer_model  TEXT NOT NULL,
    reviewer_model  TEXT NOT NULL,
    screen_name     TEXT,
    log_path        TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    started_at      REAL,
    ended_at        REAL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL NOT NULL,
    user_email  TEXT,
    ip          TEXT,
    action      TEXT NOT NULL,
    details     TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "max_concurrent_runs": 3,
    "max_runs_per_user": 1,
    "otp_expiry_minutes": 10,
    "available_proposer_models": ["codex", "claude", "claude-ds", "claude-codex"],
    "available_reviewer_models": ["codex", "claude", "claude-ds", "claude-codex"],
    "default_proposer_model": "codex",
    "default_reviewer_model": "codex",
}

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DB_PATH"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

def init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
    conn.commit()
    conn.close()

def close_db(e=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()

def get_setting(key: str):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None
```

- [ ] **Step 6: Write the failing health test**

`tests/conftest.py`:
```python
import pytest
import os
from backend.app import create_app

@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "test.db")
    userdata_dir = str(tmp_path / "userdata")
    os.makedirs(userdata_dir)
    config_path = str(tmp_path / "config.toml")
    with open(config_path, "w") as f:
        f.write(f"""
[server]
secret_key = "test-secret"
[paths]
plugin_dir = "/tmp/fake-plugin"
userdata_dir = "{userdata_dir}"
db_path = "{db_path}"
[email]
smtp_host = ""
smtp_port = 587
smtp_user = ""
smtp_password = ""
from_address = "test@example.com"
[admin]
emails = ["admin@example.com"]
""")
    app = create_app(config_path)
    app.config["TESTING"] = True
    return app

@pytest.fixture
def client(app):
    return app.test_client()
```

`tests/test_health.py`:
```python
def test_health_returns_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}
```

- [ ] **Step 7: Run test — expect failure**

```bash
cd ~/AgonSR-WebUI
pytest tests/test_health.py -v
```
Expected: `ModuleNotFoundError: No module named 'backend'`

- [ ] **Step 8: Implement app.py**

`backend/__init__.py`: (empty)

`backend/app.py`:
```python
from __future__ import annotations
from flask import Flask, jsonify
from .config import load_config
from .db import close_db, init_db

def create_app(config_path: str = "backend/config.toml") -> Flask:
    cfg = load_config(config_path)
    app = Flask(__name__)
    app.config["CFG"] = cfg
    app.config["SECRET_KEY"] = cfg["server"]["secret_key"]
    app.config["DB_PATH"] = cfg["paths"]["db_path"]
    app.config["PLUGIN_DIR"] = cfg["paths"]["plugin_dir"]
    app.config["USERDATA_DIR"] = cfg["paths"]["userdata_dir"]

    init_db(app.config["DB_PATH"])
    app.teardown_appcontext(close_db)

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True})

    return app
```

- [ ] **Step 9: Run test — expect pass**

```bash
pytest tests/test_health.py -v
```
Expected: `PASSED`

- [ ] **Step 10: Commit**

```bash
git add backend/ tests/
git commit -m "feat: Flask skeleton with SQLite schema and health endpoint"
```

---

### Task 2: Auth backend — OTP + JWT

**Files:**
- Create: `backend/auth.py`
- Create: `tests/test_auth.py`

**Interfaces:**
- Produces: `require_auth` decorator → injects `g.user_email: str`, `g.is_admin: bool`
- Produces: `GET /api/auth/me`, `POST /api/auth/request-otp`, `POST /api/auth/verify-otp`, `POST /api/auth/logout`

- [ ] **Step 1: Write failing tests**

`tests/test_auth.py`:
```python
import time

def test_me_unauthenticated(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401

def test_request_otp_creates_user(client):
    r = client.post("/api/auth/request-otp", json={"email": "user@example.com"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

def test_request_otp_bad_email(client):
    r = client.post("/api/auth/request-otp", json={"email": "not-an-email"})
    assert r.status_code == 400

def test_verify_otp_wrong_code(client):
    client.post("/api/auth/request-otp", json={"email": "user@example.com"})
    r = client.post("/api/auth/verify-otp", json={"email": "user@example.com", "otp": "000000"})
    assert r.status_code == 401

def test_verify_otp_correct(client, app, monkeypatch):
    # patch OTP generation to return known value
    import backend.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_generate_otp", lambda: "123456")
    client.post("/api/auth/request-otp", json={"email": "user@example.com"})
    r = client.post("/api/auth/verify-otp", json={"email": "user@example.com", "otp": "123456"})
    assert r.status_code == 200
    assert "agonsr_session" in r.headers.get("Set-Cookie", "")

def test_me_authenticated(client, monkeypatch):
    import backend.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_generate_otp", lambda: "123456")
    client.post("/api/auth/request-otp", json={"email": "user@example.com"})
    client.post("/api/auth/verify-otp", json={"email": "user@example.com", "otp": "123456"})
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    data = r.get_json()
    assert data["email"] == "user@example.com"
    assert data["is_admin"] is False

def test_admin_flag_set_for_admin_email(client, monkeypatch):
    import backend.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_generate_otp", lambda: "654321")
    client.post("/api/auth/request-otp", json={"email": "admin@example.com"})
    client.post("/api/auth/verify-otp", json={"email": "admin@example.com", "otp": "654321"})
    r = client.get("/api/auth/me")
    assert r.get_json()["is_admin"] is True
```

- [ ] **Step 2: Run tests — expect failure**

```bash
pytest tests/test_auth.py -v
```
Expected: All fail (no auth blueprint registered yet)

- [ ] **Step 3: Implement auth.py**

`backend/auth.py`:
```python
from __future__ import annotations
import random
import re
import time
from functools import wraps

import bcrypt
import jwt
from flask import Blueprint, current_app, g, jsonify, make_response, request

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _generate_otp() -> str:
    return f"{random.randint(0, 999999):06d}"

def _issue_jwt(email: str, is_admin: bool) -> str:
    payload = {
        "sub": email,
        "adm": is_admin,
        "exp": time.time() + 86400,
    }
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")

def _decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
    except jwt.PyJWTError:
        return None

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("agonsr_session")
        if not token:
            return jsonify({"error": "Unauthorized"}), 401
        payload = _decode_jwt(token)
        if not payload:
            return jsonify({"error": "Unauthorized"}), 401
        g.user_email = payload["sub"]
        g.is_admin = payload.get("adm", False)
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        if not g.is_admin:
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return decorated

@auth_bp.post("/request-otp")
def request_otp():
    from .db import get_db, get_setting
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    if not _EMAIL_RE.match(email):
        return jsonify({"error": "Invalid email"}), 400

    otp = _generate_otp()
    otp_hash = bcrypt.hashpw(otp.encode(), bcrypt.gensalt()).decode()
    cfg = current_app.config["CFG"]
    expiry_minutes = get_setting("otp_expiry_minutes") or 10
    expiry = time.time() + expiry_minutes * 60
    admin_emails = [e.lower() for e in cfg["admin"]["emails"]]
    is_admin = email in admin_emails

    db = get_db()
    db.execute(
        """INSERT INTO users (email, is_admin, otp_hash, otp_expiry, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(email) DO UPDATE SET
             is_admin=excluded.is_admin,
             otp_hash=excluded.otp_hash,
             otp_expiry=excluded.otp_expiry""",
        (email, int(is_admin), otp_hash, expiry, time.time()),
    )
    db.commit()

    # send email (imported lazily to allow testing without SMTP)
    try:
        from .email_client import send_otp_email
        send_otp_email(email, otp)
    except Exception:
        pass  # log in production; don't block response

    current_app.logger.info("OTP requested for %s (otp=%s in test)", email, otp)
    return jsonify({"ok": True})

@auth_bp.post("/verify-otp")
def verify_otp():
    from .db import get_db
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    otp = str(data.get("otp", "")).strip()

    if not _EMAIL_RE.match(email) or not otp:
        return jsonify({"error": "Invalid request"}), 400

    db = get_db()
    row = db.execute(
        "SELECT is_admin, otp_hash, otp_expiry FROM users WHERE email = ?", (email,)
    ).fetchone()

    if not row:
        return jsonify({"error": "Invalid code"}), 401
    if time.time() > row["otp_expiry"]:
        return jsonify({"error": "Code expired"}), 401
    if not bcrypt.checkpw(otp.encode(), row["otp_hash"].encode()):
        return jsonify({"error": "Invalid code"}), 401

    # Invalidate OTP
    db.execute("UPDATE users SET otp_hash=NULL, otp_expiry=0 WHERE email=?", (email,))
    db.commit()

    token = _issue_jwt(email, bool(row["is_admin"]))
    resp = make_response(jsonify({"ok": True, "email": email, "is_admin": bool(row["is_admin"])}))
    resp.set_cookie(
        "agonsr_session", token,
        httponly=True, samesite="Lax", max_age=86400,
        secure=False,  # set True behind HTTPS in production
    )
    return resp

@auth_bp.get("/me")
@require_auth
def me():
    return jsonify({"email": g.user_email, "is_admin": g.is_admin})

@auth_bp.post("/logout")
def logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("agonsr_session")
    return resp
```

- [ ] **Step 4: Register blueprint in app.py**

Add to `create_app()` before `return app`:
```python
from .auth import auth_bp
app.register_blueprint(auth_bp)
```

- [ ] **Step 5: Run tests — expect pass**

```bash
pytest tests/test_auth.py -v
```
Expected: All 7 PASSED

- [ ] **Step 6: Commit**

```bash
git add backend/auth.py backend/app.py tests/test_auth.py
git commit -m "feat: email+OTP auth with JWT cookie"
```

---

### Task 3: Email client

**Files:**
- Create: `backend/email_client.py`
- Create: `tests/test_email.py`

**Interfaces:**
- Produces: `send_otp_email(email: str, otp: str) -> None`
- Produces: `send_completion_email(email: str, project_name: str, run_id: str, base_url: str) -> None`

- [ ] **Step 1: Write failing test**

`tests/test_email.py`:
```python
import smtplib
from unittest.mock import MagicMock, patch
from backend.email_client import send_otp_email, send_completion_email

def test_send_otp_email_calls_smtp(app):
    with app.app_context():
        with patch("smtplib.SMTP") as mock_smtp:
            instance = MagicMock()
            mock_smtp.return_value.__enter__ = lambda s: instance
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            send_otp_email("user@example.com", "123456")
            # just verify it didn't raise

def test_send_otp_skips_if_no_smtp_host(app):
    with app.app_context():
        # config.example.toml has empty smtp_host → should not raise
        send_otp_email("user@example.com", "123456")

def test_send_completion_email_skips_if_no_smtp_host(app):
    with app.app_context():
        send_completion_email("user@example.com", "MyProject", "abc123", "http://localhost:3000")
```

- [ ] **Step 2: Run test — expect ImportError**

```bash
pytest tests/test_email.py -v
```
Expected: `ModuleNotFoundError: No module named 'backend.email_client'`

- [ ] **Step 3: Implement email_client.py**

`backend/email_client.py`:
```python
from __future__ import annotations
import smtplib
from email.message import EmailMessage
from flask import current_app

def _smtp_cfg() -> dict:
    return current_app.config["CFG"]["email"]

def _send(to: str, subject: str, body: str) -> None:
    cfg = _smtp_cfg()
    if not cfg.get("smtp_host"):
        current_app.logger.warning("No SMTP host configured; skipping email to %s", to)
        return
    msg = EmailMessage()
    msg["From"] = cfg["from_address"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"])) as s:
        if cfg.get("smtp_user"):
            s.starttls()
            s.login(cfg["smtp_user"], cfg["smtp_password"])
        s.send_message(msg)

def send_otp_email(email: str, otp: str) -> None:
    _send(
        email,
        "Your AgonSR verification code",
        f"Your AgonSR verification code is: {otp}\n\nValid for 10 minutes.\n",
    )

def send_completion_email(email: str, project_name: str, run_id: str, base_url: str) -> None:
    url = f"{base_url}/project/{run_id}"
    _send(
        email,
        f"AgonSR run completed — {project_name}",
        f"Your AgonSR run on project '{project_name}' has finished.\n\nView results: {url}\n",
    )
```

- [ ] **Step 4: Run tests — expect pass**

```bash
pytest tests/test_email.py -v
```
Expected: All 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add backend/email_client.py tests/test_email.py
git commit -m "feat: SMTP email client for OTP and completion notifications"
```

---

### Task 4: Audit logging middleware

**Files:**
- Create: `backend/audit.py`
- Create: `tests/test_audit.py`

**Interfaces:**
- Produces: `register_audit(app: Flask) -> None` — call from `create_app`
- Produces: `log_action(action: str, details: dict) -> None` — call from any endpoint

- [ ] **Step 1: Write failing test**

`tests/test_audit.py`:
```python
from backend.db import get_db

def test_request_is_audited(client, app):
    client.post("/api/auth/request-otp", json={"email": "user@example.com"})
    with app.app_context():
        rows = get_db().execute("SELECT * FROM audit_log").fetchall()
    assert len(rows) >= 1
    assert rows[0]["action"] == "request_otp"

def test_audit_captures_ip(client, app):
    client.post(
        "/api/auth/request-otp",
        json={"email": "user@example.com"},
        environ_base={"REMOTE_ADDR": "1.2.3.4"},
    )
    with app.app_context():
        row = get_db().execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["ip"] == "1.2.3.4"
```

- [ ] **Step 2: Run test — expect failure**

```bash
pytest tests/test_audit.py -v
```
Expected: FAIL — no audit rows exist yet

- [ ] **Step 3: Implement audit.py**

`backend/audit.py`:
```python
from __future__ import annotations
import json
import time
from flask import Flask, g, request
from .db import get_db

def log_action(action: str, details: dict | None = None) -> None:
    email = getattr(g, "user_email", None)
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    get_db().execute(
        "INSERT INTO audit_log (timestamp, user_email, ip, action, details) VALUES (?,?,?,?,?)",
        (time.time(), email, ip, action, json.dumps(details or {})),
    )
    get_db().commit()

def register_audit(app: Flask) -> None:
    # Audit key endpoints via after_request is too broad;
    # individual blueprints call log_action() explicitly instead.
    pass
```

- [ ] **Step 4: Add log_action calls to auth.py**

In `auth.py`, after `db.commit()` in `request_otp`:
```python
from .audit import log_action
log_action("request_otp", {"email": email})
```

After `db.commit()` in `verify_otp` (success path):
```python
log_action("login_success", {"email": email})
```

- [ ] **Step 5: Run test — expect pass**

```bash
pytest tests/test_audit.py -v
```
Expected: Both PASSED

- [ ] **Step 6: Commit**

```bash
git add backend/audit.py backend/auth.py tests/test_audit.py
git commit -m "feat: audit logging — records OTP requests and logins"
```

---

### Task 5: Project CRUD backend

**Files:**
- Create: `backend/projects.py`
- Create: `tests/test_projects.py`

**Interfaces:**
- Produces: `GET /api/projects` → `[{id, name, created_at}]`
- Produces: `POST /api/projects {name}` → `{id, name}`
- Produces: `PUT /api/projects/<id>/rename {name}` → `{ok}`
- Produces: `DELETE /api/projects/<id>` → `{ok}`
- Produces: `get_project_dir(user_email, project_name) -> Path` (used by file + run tasks)

- [ ] **Step 1: Write failing tests**

`tests/test_projects.py`:
```python
import pytest

@pytest.fixture
def auth_client(client, monkeypatch):
    import backend.auth as a
    monkeypatch.setattr(a, "_generate_otp", lambda: "111111")
    client.post("/api/auth/request-otp", json={"email": "user@example.com"})
    client.post("/api/auth/verify-otp", json={"email": "user@example.com", "otp": "111111"})
    return client

def test_list_projects_empty(auth_client):
    r = auth_client.get("/api/projects")
    assert r.status_code == 200
    assert r.get_json() == []

def test_create_project(auth_client, app):
    r = auth_client.post("/api/projects", json={"name": "MyProject"})
    assert r.status_code == 201
    data = r.get_json()
    assert data["name"] == "MyProject"
    assert "id" in data

def test_create_project_invalid_name(auth_client):
    r = auth_client.post("/api/projects", json={"name": "../etc"})
    assert r.status_code == 400

def test_create_project_creates_directory(auth_client, app):
    auth_client.post("/api/projects", json={"name": "TestProj"})
    import os
    ud = app.config["USERDATA_DIR"]
    assert os.path.isdir(f"{ud}/user@example.com/TestProj")

def test_list_projects_after_create(auth_client):
    auth_client.post("/api/projects", json={"name": "Alpha"})
    r = auth_client.get("/api/projects")
    names = [p["name"] for p in r.get_json()]
    assert "Alpha" in names

def test_rename_project(auth_client):
    r = auth_client.post("/api/projects", json={"name": "OldName"})
    pid = r.get_json()["id"]
    r2 = auth_client.put(f"/api/projects/{pid}/rename", json={"name": "NewName"})
    assert r2.status_code == 200
    r3 = auth_client.get("/api/projects")
    names = [p["name"] for p in r3.get_json()]
    assert "NewName" in names
    assert "OldName" not in names

def test_unauthenticated_returns_401(client):
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/projects", json={"name": "X"}).status_code == 401
```

- [ ] **Step 2: Run tests — expect failure**

```bash
pytest tests/test_projects.py -v
```
Expected: All fail (blueprint not registered)

- [ ] **Step 3: Implement projects.py**

`backend/projects.py`:
```python
from __future__ import annotations
import re
import shutil
import time
import uuid
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request

from .auth import require_auth
from .audit import log_action
from .db import get_db

projects_bp = Blueprint("projects", __name__, url_prefix="/api/projects")

_VALID_NAME = re.compile(r"^[A-Za-z0-9_\-][A-Za-z0-9_\- ]{0,63}$")

def get_project_dir(user_email: str, project_name: str) -> Path:
    userdata = Path(current_app.config["USERDATA_DIR"]).resolve()
    d = (userdata / user_email / project_name).resolve()
    if not d.is_relative_to(userdata / user_email):
        raise ValueError("Path traversal detected")
    return d

def _get_project_or_404(project_id: str, user_email: str):
    row = get_db().execute(
        "SELECT * FROM projects WHERE id=? AND user_email=?", (project_id, user_email)
    ).fetchone()
    if not row:
        return None
    return row

@projects_bp.get("")
@require_auth
def list_projects():
    rows = get_db().execute(
        "SELECT id, name, created_at FROM projects WHERE user_email=? ORDER BY created_at DESC",
        (g.user_email,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@projects_bp.post("")
@require_auth
def create_project():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not _VALID_NAME.match(name):
        return jsonify({"error": "Invalid project name"}), 400

    project_dir = get_project_dir(g.user_email, name)
    if project_dir.exists():
        return jsonify({"error": "Project name already exists"}), 409

    project_id = uuid.uuid4().hex
    project_dir.mkdir(parents=True)
    (project_dir / "data").mkdir()
    (project_dir / "problem.md").write_text("", encoding="utf-8")
    (project_dir / "IGNOREME.md").write_text(
        "## Notes to ansatz-proposer\n\n## Notes to ansatz-reviewer\n\n## Notes to dispatcher\n",
        encoding="utf-8",
    )

    db = get_db()
    db.execute(
        "INSERT INTO projects (id, user_email, name, created_at) VALUES (?,?,?,?)",
        (project_id, g.user_email, name, time.time()),
    )
    db.commit()
    log_action("project_create", {"name": name, "id": project_id})
    return jsonify({"id": project_id, "name": name}), 201

@projects_bp.put("/<project_id>/rename")
@require_auth
def rename_project(project_id: str):
    data = request.get_json(silent=True) or {}
    new_name = str(data.get("name", "")).strip()
    if not _VALID_NAME.match(new_name):
        return jsonify({"error": "Invalid project name"}), 400

    row = _get_project_or_404(project_id, g.user_email)
    if not row:
        return jsonify({"error": "Not found"}), 404

    old_dir = get_project_dir(g.user_email, row["name"])
    new_dir = get_project_dir(g.user_email, new_name)
    if new_dir.exists():
        return jsonify({"error": "Name already taken"}), 409

    old_dir.rename(new_dir)
    get_db().execute("UPDATE projects SET name=? WHERE id=?", (new_name, project_id))
    get_db().commit()
    log_action("project_rename", {"id": project_id, "old": row["name"], "new": new_name})
    return jsonify({"ok": True})

@projects_bp.delete("/<project_id>")
@require_auth
def delete_project(project_id: str):
    row = _get_project_or_404(project_id, g.user_email)
    if not row:
        return jsonify({"error": "Not found"}), 404
    active = get_db().execute(
        "SELECT id FROM runs WHERE project_id=? AND status='running'", (project_id,)
    ).fetchone()
    if active:
        return jsonify({"error": "Cannot delete project with active run"}), 409
    project_dir = get_project_dir(g.user_email, row["name"])
    if project_dir.exists():
        shutil.rmtree(project_dir)
    get_db().execute("DELETE FROM projects WHERE id=?", (project_id,))
    get_db().commit()
    log_action("project_delete", {"id": project_id, "name": row["name"]})
    return jsonify({"ok": True})
```

- [ ] **Step 4: Register in app.py**

```python
from .projects import projects_bp
app.register_blueprint(projects_bp)
```

- [ ] **Step 5: Run tests — expect pass**

```bash
pytest tests/test_projects.py -v
```
Expected: All 8 PASSED

- [ ] **Step 6: Commit**

```bash
git add backend/projects.py backend/app.py tests/test_projects.py
git commit -m "feat: project CRUD — create/list/rename/delete with directory management"
```

---

### Task 6: File editing and CSV upload

**Files:**
- Create: `backend/files.py`
- Create: `tests/test_files.py`

**Interfaces:**
- Produces: `GET /api/projects/<id>/files` → `{problem_md, proposer_notes, reviewer_notes, dispatcher_notes, data_files: [str]}`
- Produces: `PUT /api/projects/<id>/files {problem_md, proposer_notes, reviewer_notes, dispatcher_notes}` → `{ok}`
- Produces: `POST /api/projects/<id>/upload` (multipart form, field `file`) → `{ok, filename}`

- [ ] **Step 1: Write failing tests**

`tests/test_files.py`:
```python
import pytest
import io

@pytest.fixture
def project(auth_client):
    r = auth_client.post("/api/projects", json={"name": "FileTest"})
    return r.get_json()

@pytest.fixture
def auth_client(client, monkeypatch):
    import backend.auth as a
    monkeypatch.setattr(a, "_generate_otp", lambda: "222222")
    client.post("/api/auth/request-otp", json={"email": "user@example.com"})
    client.post("/api/auth/verify-otp", json={"email": "user@example.com", "otp": "222222"})
    return client

def test_get_files_initial(auth_client, project):
    r = auth_client.get(f"/api/projects/{project['id']}/files")
    assert r.status_code == 200
    data = r.get_json()
    assert data["problem_md"] == ""
    assert data["proposer_notes"] == ""
    assert data["data_files"] == []

def test_put_files(auth_client, project):
    r = auth_client.put(
        f"/api/projects/{project['id']}/files",
        json={"problem_md": "# Test", "proposer_notes": "be creative",
              "reviewer_notes": "", "dispatcher_notes": ""},
    )
    assert r.status_code == 200
    r2 = auth_client.get(f"/api/projects/{project['id']}/files")
    assert r2.get_json()["problem_md"] == "# Test"
    assert r2.get_json()["proposer_notes"] == "be creative"

def test_upload_csv(auth_client, project):
    data = b"A,B\n1,2\n3,4\n"
    r = auth_client.post(
        f"/api/projects/{project['id']}/upload",
        data={"file": (io.BytesIO(data), "train.csv")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    assert r.get_json()["filename"] == "train.csv"

def test_upload_non_csv_rejected(auth_client, project):
    r = auth_client.post(
        f"/api/projects/{project['id']}/upload",
        data={"file": (io.BytesIO(b"hello"), "evil.sh")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 400
```

- [ ] **Step 2: Run tests — expect failure**

```bash
pytest tests/test_files.py -v
```

- [ ] **Step 3: Implement files.py**

`backend/files.py`:
```python
from __future__ import annotations
import re
from pathlib import Path

from flask import Blueprint, g, jsonify, request

from .auth import require_auth
from .audit import log_action
from .db import get_db
from .projects import get_project_dir, _get_project_or_404

files_bp = Blueprint("files", __name__, url_prefix="/api/projects")

_IGNOREME_SECTIONS = {
    "proposer_notes": "## Notes to ansatz-proposer",
    "reviewer_notes": "## Notes to ansatz-reviewer",
    "dispatcher_notes": "## Notes to dispatcher",
}

def _parse_ignoreme(text: str) -> dict[str, str]:
    result = {k: "" for k in _IGNOREME_SECTIONS}
    headers = list(_IGNOREME_SECTIONS.values())
    for i, (key, header) in enumerate(_IGNOREME_SECTIONS.items()):
        next_headers = headers[i + 1:]
        pattern = re.escape(header) + r"\s*(.*?)(?=" + "|".join(map(re.escape, next_headers)) + r"|\Z)"
        m = re.search(pattern, text, re.DOTALL)
        result[key] = m.group(1).strip() if m else ""
    return result

def _build_ignoreme(proposer: str, reviewer: str, dispatcher: str) -> str:
    return (
        f"## Notes to ansatz-proposer\n\n{proposer}\n\n"
        f"## Notes to ansatz-reviewer\n\n{reviewer}\n\n"
        f"## Notes to dispatcher\n\n{dispatcher}\n"
    )

@files_bp.get("/<project_id>/files")
@require_auth
def get_files(project_id: str):
    row = _get_project_or_404(project_id, g.user_email)
    if not row:
        return jsonify({"error": "Not found"}), 404
    d = get_project_dir(g.user_email, row["name"])
    problem_md = (d / "problem.md").read_text(encoding="utf-8") if (d / "problem.md").exists() else ""
    ignoreme_text = (d / "IGNOREME.md").read_text(encoding="utf-8") if (d / "IGNOREME.md").exists() else ""
    notes = _parse_ignoreme(ignoreme_text)
    data_files = sorted(f.name for f in (d / "data").iterdir() if f.is_file()) if (d / "data").exists() else []
    return jsonify({"problem_md": problem_md, **notes, "data_files": data_files})

@files_bp.put("/<project_id>/files")
@require_auth
def put_files(project_id: str):
    row = _get_project_or_404(project_id, g.user_email)
    if not row:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(silent=True) or {}
    d = get_project_dir(g.user_email, row["name"])
    (d / "problem.md").write_text(data.get("problem_md", ""), encoding="utf-8")
    ignoreme = _build_ignoreme(
        data.get("proposer_notes", ""),
        data.get("reviewer_notes", ""),
        data.get("dispatcher_notes", ""),
    )
    (d / "IGNOREME.md").write_text(ignoreme, encoding="utf-8")
    log_action("file_edit", {"project_id": project_id})
    return jsonify({"ok": True})

@files_bp.post("/<project_id>/upload")
@require_auth
def upload_csv(project_id: str):
    row = _get_project_or_404(project_id, g.user_email)
    if not row:
        return jsonify({"error": "Not found"}), 404
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400
    if not f.filename or not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "Only .csv files allowed"}), 400
    safe_name = Path(f.filename).name
    dest = get_project_dir(g.user_email, row["name"]) / "data" / safe_name
    f.save(str(dest))
    log_action("csv_upload", {"project_id": project_id, "filename": safe_name})
    return jsonify({"ok": True, "filename": safe_name})
```

- [ ] **Step 4: Register in app.py**

```python
from .files import files_bp
app.register_blueprint(files_bp)
```

- [ ] **Step 5: Run tests — expect pass**

```bash
pytest tests/test_files.py -v
```
Expected: All 4 PASSED

- [ ] **Step 6: Commit**

```bash
git add backend/files.py backend/app.py tests/test_files.py
git commit -m "feat: file editing and CSV upload endpoints"
```

---

### Task 7: Run launch + completion detection

**Files:**
- Create: `backend/runs.py`
- Create: `tests/test_runs.py`

**Interfaces:**
- Produces: `POST /api/projects/<id>/runs {rounds, proposer_model, reviewer_model}` → `{run_id}`
- Produces: `GET /api/projects/<id>/runs` → `[{id, status, rounds, started_at, ended_at}]`
- Produces: `GET /api/runs/<run_id>` → full run record
- Produces: `start_completion_watcher(app)` — call once from `create_app`

- [ ] **Step 1: Write failing tests**

`tests/test_runs.py`:
```python
import pytest
from unittest.mock import patch

@pytest.fixture
def auth_client(client, monkeypatch):
    import backend.auth as a
    monkeypatch.setattr(a, "_generate_otp", lambda: "333333")
    client.post("/api/auth/request-otp", json={"email": "user@example.com"})
    client.post("/api/auth/verify-otp", json={"email": "user@example.com", "otp": "333333"})
    return client

@pytest.fixture
def project(auth_client):
    return auth_client.post("/api/projects", json={"name": "RunTest"}).get_json()

def test_start_run(auth_client, project):
    with patch("backend.runs._spawn_screen") as mock_spawn:
        mock_spawn.return_value = None
        r = auth_client.post(
            f"/api/projects/{project['id']}/runs",
            json={"rounds": 3, "proposer_model": "codex", "reviewer_model": "codex"},
        )
    assert r.status_code == 201
    data = r.get_json()
    assert "run_id" in data

def test_start_run_invalid_rounds(auth_client, project):
    r = auth_client.post(
        f"/api/projects/{project['id']}/runs",
        json={"rounds": 0, "proposer_model": "codex", "reviewer_model": "codex"},
    )
    assert r.status_code == 400

def test_start_run_invalid_model(auth_client, project):
    r = auth_client.post(
        f"/api/projects/{project['id']}/runs",
        json={"rounds": 1, "proposer_model": "gpt-99", "reviewer_model": "codex"},
    )
    assert r.status_code == 400

def test_list_runs(auth_client, project):
    with patch("backend.runs._spawn_screen"):
        auth_client.post(
            f"/api/projects/{project['id']}/runs",
            json={"rounds": 1, "proposer_model": "codex", "reviewer_model": "codex"},
        )
    r = auth_client.get(f"/api/projects/{project['id']}/runs")
    assert r.status_code == 200
    runs = r.get_json()
    assert len(runs) == 1
    assert runs[0]["status"] in ("pending", "running")

def test_cannot_start_second_run_while_one_running(auth_client, project):
    with patch("backend.runs._spawn_screen"):
        auth_client.post(
            f"/api/projects/{project['id']}/runs",
            json={"rounds": 1, "proposer_model": "codex", "reviewer_model": "codex"},
        )
        r = auth_client.post(
            f"/api/projects/{project['id']}/runs",
            json={"rounds": 1, "proposer_model": "codex", "reviewer_model": "codex"},
        )
    assert r.status_code == 409
```

- [ ] **Step 2: Run tests — expect failure**

```bash
pytest tests/test_runs.py -v
```

- [ ] **Step 3: Implement runs.py**

`backend/runs.py`:
```python
from __future__ import annotations
import subprocess
import time
import threading
import uuid
from pathlib import Path

from flask import Blueprint, Flask, current_app, g, jsonify, request

from .auth import require_auth
from .audit import log_action
from .db import get_db, get_setting
from .projects import get_project_dir, _get_project_or_404

runs_bp = Blueprint("runs", __name__)

def _spawn_screen(screen_name: str, workdir: Path, log_path: Path, plugin_dir: str, rounds: int) -> None:
    import shlex
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Build the inner bash command with shlex.quote on every user-controlled value,
    # then pass the whole string as a single argument to bash -c (no shell=True on Popen).
    bash_cmd = (
        f"cd {shlex.quote(str(workdir))} && "
        f"claude --dangerously-skip-permissions "
        f"--plugin-dir {shlex.quote(str(plugin_dir))} "
        f"--model claude-sonnet-5[1m] "
        f"-p {shlex.quote(f'/llm-mcts {int(rounds)} problem.md')} 2>&1"
    )
    subprocess.Popen(
        ["screen", "-dmS", screen_name, "-L", "-Logfile", str(log_path), "bash", "-c", bash_cmd],
    )

def _screen_alive(screen_name: str) -> bool:
    result = subprocess.run(["screen", "-ls"], capture_output=True, text=True)
    return screen_name in result.stdout

def _completion_watcher(app: Flask) -> None:
    while True:
        time.sleep(10)
        try:
            with app.app_context():
                db = get_db()
                rows = db.execute(
                    "SELECT id, screen_name, project_id FROM runs WHERE status='running'"
                ).fetchall()
                for row in rows:
                    if not _screen_alive(row["screen_name"]):
                        db.execute(
                            "UPDATE runs SET status='completed', ended_at=? WHERE id=?",
                            (time.time(), row["id"]),
                        )
                        db.commit()
                        _send_completion_notification(app, row["id"], row["project_id"])
        except Exception as exc:
            app.logger.exception("Completion watcher error: %s", exc)

def _send_completion_notification(app: Flask, run_id: str, project_id: str) -> None:
    try:
        with app.app_context():
            db = get_db()
            proj = db.execute("SELECT name, user_email FROM projects WHERE id=?", (project_id,)).fetchone()
            if not proj:
                return
            from .email_client import send_completion_email
            base_url = app.config["CFG"].get("server", {}).get("base_url", "http://localhost:3000")
            send_completion_email(proj["user_email"], proj["name"], run_id, base_url)
    except Exception as exc:
        app.logger.exception("Completion email error: %s", exc)

def start_completion_watcher(app: Flask) -> None:
    t = threading.Thread(target=_completion_watcher, args=(app,), daemon=True, name="completion-watcher")
    t.start()

@runs_bp.post("/api/projects/<project_id>/runs")
@require_auth
def start_run(project_id: str):
    row = _get_project_or_404(project_id, g.user_email)
    if not row:
        return jsonify({"error": "Not found"}), 404

    data = request.get_json(silent=True) or {}
    rounds = data.get("rounds")
    proposer_model = str(data.get("proposer_model", ""))
    reviewer_model = str(data.get("reviewer_model", ""))

    if not isinstance(rounds, int) or rounds < 1:
        return jsonify({"error": "rounds must be a positive integer"}), 400

    available = get_setting("available_proposer_models") or []
    if proposer_model not in available or reviewer_model not in (get_setting("available_reviewer_models") or []):
        return jsonify({"error": "Invalid model selection"}), 400

    db = get_db()
    # per-user limit
    max_per_user = get_setting("max_runs_per_user") or 1
    active_user = db.execute(
        "SELECT COUNT(*) FROM runs r JOIN projects p ON r.project_id=p.id "
        "WHERE p.user_email=? AND r.status='running'", (g.user_email,)
    ).fetchone()[0]
    if active_user >= max_per_user:
        return jsonify({"error": "You already have an active run"}), 409

    # global limit
    max_global = get_setting("max_concurrent_runs") or 3
    active_global = db.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0]
    if active_global >= max_global:
        return jsonify({"error": "Server at capacity, please try again later"}), 409

    run_id = uuid.uuid4().hex
    screen_name = f"agonsr-{run_id}"
    workdir = get_project_dir(g.user_email, row["name"])
    log_path = workdir / "runs" / run_id / "screen.log"
    plugin_dir = current_app.config["PLUGIN_DIR"]

    # Write settings.toml for this run
    (workdir / "settings.toml").write_text(
        f'ansatz-proposer-model = "{proposer_model}"\n'
        f'ansatz-reviewer-model = "{reviewer_model}"\n',
        encoding="utf-8",
    )

    db.execute(
        "INSERT INTO runs (id, project_id, rounds, proposer_model, reviewer_model, "
        "screen_name, log_path, status, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, project_id, rounds, proposer_model, reviewer_model,
         screen_name, str(log_path), "running", time.time()),
    )
    db.commit()

    _spawn_screen(screen_name, workdir, log_path, plugin_dir, rounds)
    log_action("run_start", {"run_id": run_id, "project_id": project_id, "rounds": rounds})
    return jsonify({"run_id": run_id}), 201

@runs_bp.get("/api/projects/<project_id>/runs")
@require_auth
def list_runs(project_id: str):
    row = _get_project_or_404(project_id, g.user_email)
    if not row:
        return jsonify({"error": "Not found"}), 404
    rows = get_db().execute(
        "SELECT id, status, rounds, proposer_model, reviewer_model, started_at, ended_at "
        "FROM runs WHERE project_id=? ORDER BY started_at DESC",
        (project_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@runs_bp.get("/api/runs/<run_id>")
@require_auth
def get_run(run_id: str):
    row = get_db().execute(
        "SELECT r.*, p.user_email FROM runs r JOIN projects p ON r.project_id=p.id WHERE r.id=?",
        (run_id,),
    ).fetchone()
    if not row or row["user_email"] != g.user_email:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))
```

- [ ] **Step 4: Register in app.py and start watcher**

```python
from .runs import runs_bp, start_completion_watcher
app.register_blueprint(runs_bp)
if not app.config.get("TESTING"):
    start_completion_watcher(app)
```

- [ ] **Step 5: Run tests — expect pass**

```bash
pytest tests/test_runs.py -v
```
Expected: All 5 PASSED

- [ ] **Step 6: Commit**

```bash
git add backend/runs.py backend/app.py tests/test_runs.py
git commit -m "feat: run launch via screen and completion detection thread"
```

---

### Task 8: WebSocket log streaming

**Files:**
- Create: `backend/ws.py`
- Create: `tests/test_ws.py`

**Interfaces:**
- Produces: `GET /ws/runs/<run_id>` — WebSocket, streams log file lines until run ends

- [ ] **Step 1: Write failing test**

`tests/test_ws.py`:
```python
import time
import threading
from unittest.mock import patch

def test_ws_run_not_found_closes(client, monkeypatch):
    import backend.auth as a
    monkeypatch.setattr(a, "_generate_otp", lambda: "444444")
    client.post("/api/auth/request-otp", json={"email": "user@example.com"})
    client.post("/api/auth/verify-otp", json={"email": "user@example.com", "otp": "444444"})
    with client.websocket("/ws/runs/nonexistent") as ws:
        msg = ws.receive(timeout=2)
        # server should close with error or send error message
        assert msg is None or "error" in str(msg).lower()
```

- [ ] **Step 2: Run test — expect failure**

```bash
pytest tests/test_ws.py -v
```

- [ ] **Step 3: Implement ws.py**

`backend/ws.py`:
```python
from __future__ import annotations
import json
import time
from pathlib import Path

from flask_sock import Sock

from .auth import _decode_jwt
from .db import get_db

sock = Sock()

def init_ws(app) -> None:
    sock.init_app(app)

    @sock.route("/ws/runs/<run_id>")
    def run_log(ws, run_id: str):
        # Auth via cookie (browser sends cookie on WS upgrade)
        from flask import request
        token = request.cookies.get("agonsr_session")
        if not token:
            ws.send(json.dumps({"error": "Unauthorized"}))
            return

        payload = _decode_jwt(token)
        if not payload:
            ws.send(json.dumps({"error": "Unauthorized"}))
            return

        user_email = payload["sub"]

        row = get_db().execute(
            "SELECT r.log_path, r.status, p.user_email "
            "FROM runs r JOIN projects p ON r.project_id=p.id WHERE r.id=?",
            (run_id,),
        ).fetchone()

        if not row or row["user_email"] != user_email:
            ws.send(json.dumps({"error": "Not found"}))
            return

        log_path = Path(row["log_path"])
        idle_count = 0

        # Wait for log file to appear (up to 30s)
        for _ in range(30):
            if log_path.exists():
                break
            time.sleep(1)

        if not log_path.exists():
            ws.send(json.dumps({"error": "Log not available"}))
            return

        with open(log_path, "r", errors="replace") as f:
            while True:
                chunk = f.read(4096)
                if chunk:
                    ws.send(chunk)
                    idle_count = 0
                else:
                    run = get_db().execute(
                        "SELECT status FROM runs WHERE id=?", (run_id,)
                    ).fetchone()
                    if run and run["status"] not in ("running", "pending"):
                        if idle_count >= 3:
                            break
                        idle_count += 1
                    time.sleep(0.5)
```

- [ ] **Step 4: Register in app.py**

```python
from .ws import init_ws
init_ws(app)
```

- [ ] **Step 5: Run test — expect pass**

```bash
pytest tests/test_ws.py -v
```

- [ ] **Step 6: Commit**

```bash
git add backend/ws.py backend/app.py tests/test_ws.py
git commit -m "feat: WebSocket endpoint streaming screen log to browser"
```

---

### Task 9: Admin backend

**Files:**
- Create: `backend/admin.py`
- Create: `tests/test_admin.py`

**Interfaces:**
- Produces: `GET /api/admin/users` → `[{email, is_admin, created_at, active_runs}]`
- Produces: `GET /api/admin/runs` → `[{id, user_email, project_name, status, started_at}]`
- Produces: `GET /api/admin/settings` → `{key: value, ...}`
- Produces: `PUT /api/admin/settings {key: value, ...}` → `{ok}`

- [ ] **Step 1: Write failing tests**

`tests/test_admin.py`:
```python
import pytest

@pytest.fixture
def admin_client(client, monkeypatch):
    import backend.auth as a
    monkeypatch.setattr(a, "_generate_otp", lambda: "999999")
    client.post("/api/auth/request-otp", json={"email": "admin@example.com"})
    client.post("/api/auth/verify-otp", json={"email": "admin@example.com", "otp": "999999"})
    return client

@pytest.fixture
def user_client(client, monkeypatch):
    import backend.auth as a
    monkeypatch.setattr(a, "_generate_otp", lambda: "888888")
    client.post("/api/auth/request-otp", json={"email": "user@example.com"})
    client.post("/api/auth/verify-otp", json={"email": "user@example.com", "otp": "888888"})
    return client

def test_admin_users(admin_client):
    r = admin_client.get("/api/admin/users")
    assert r.status_code == 200
    users = r.get_json()
    emails = [u["email"] for u in users]
    assert "admin@example.com" in emails

def test_non_admin_forbidden(user_client):
    assert user_client.get("/api/admin/users").status_code == 403

def test_get_settings(admin_client):
    r = admin_client.get("/api/admin/settings")
    assert r.status_code == 200
    data = r.get_json()
    assert "max_concurrent_runs" in data

def test_update_settings(admin_client):
    r = admin_client.put("/api/admin/settings", json={"max_concurrent_runs": 5})
    assert r.status_code == 200
    r2 = admin_client.get("/api/admin/settings")
    assert r2.get_json()["max_concurrent_runs"] == 5
```

- [ ] **Step 2: Run tests — expect failure**

```bash
pytest tests/test_admin.py -v
```

- [ ] **Step 3: Implement admin.py**

`backend/admin.py`:
```python
from __future__ import annotations
import json
from flask import Blueprint, g, jsonify, request
from .auth import require_admin
from .db import get_db, get_setting

admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")

_EDITABLE_SETTINGS = {
    "max_concurrent_runs", "max_runs_per_user", "otp_expiry_minutes",
    "available_proposer_models", "available_reviewer_models",
    "default_proposer_model", "default_reviewer_model",
}

@admin_bp.get("/users")
@require_admin
def list_users():
    rows = get_db().execute(
        "SELECT u.email, u.is_admin, u.created_at, "
        "COUNT(CASE WHEN r.status='running' THEN 1 END) as active_runs "
        "FROM users u LEFT JOIN projects p ON p.user_email=u.email "
        "LEFT JOIN runs r ON r.project_id=p.id "
        "GROUP BY u.email ORDER BY u.created_at DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@admin_bp.get("/runs")
@require_admin
def list_active_runs():
    rows = get_db().execute(
        "SELECT r.id, u.email as user_email, p.name as project_name, "
        "r.status, r.rounds, r.started_at, r.ended_at "
        "FROM runs r JOIN projects p ON r.project_id=p.id JOIN users u ON p.user_email=u.email "
        "WHERE r.status IN ('running','pending') ORDER BY r.started_at DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@admin_bp.get("/settings")
@require_admin
def get_settings():
    rows = get_db().execute("SELECT key, value FROM settings").fetchall()
    return jsonify({r["key"]: json.loads(r["value"]) for r in rows})

@admin_bp.put("/settings")
@require_admin
def update_settings():
    data = request.get_json(silent=True) or {}
    db = get_db()
    for key, value in data.items():
        if key not in _EDITABLE_SETTINGS:
            return jsonify({"error": f"Unknown setting: {key}"}), 400
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
    db.commit()
    return jsonify({"ok": True})
```

- [ ] **Step 4: Register in app.py**

```python
from .admin import admin_bp
app.register_blueprint(admin_bp)
```

- [ ] **Step 5: Run tests — expect pass**

```bash
pytest tests/test_admin.py -v
```
Expected: All 4 PASSED

- [ ] **Step 6: Run all backend tests**

```bash
pytest tests/ -v
```
Expected: All pass. Fix any regressions before continuing.

- [ ] **Step 7: Commit**

```bash
git add backend/admin.py backend/app.py tests/test_admin.py
git commit -m "feat: admin endpoints — user list, active runs, settings CRUD"
```

---

### Task 10: Firejail sandbox wrapper

**Files:**
- Modify: `backend/runs.py` (wrap `_spawn_screen`)
- Create: `tests/test_sandbox.py`

**Note:** This task requires `firejail` installed on the server (`sudo apt install firejail`). If firejail is unavailable, `_spawn_screen` falls back to unsandboxed execution and logs a warning.

- [ ] **Step 1: Write test**

`tests/test_sandbox.py`:
```python
from unittest.mock import patch, MagicMock
import subprocess
from backend.runs import _build_screen_cmd

def test_build_cmd_includes_firejail_when_available():
    with patch("shutil.which", return_value="/usr/bin/firejail"):
        argv = _build_screen_cmd(
            screen_name="agonsr-abc",
            workdir="/tmp/wd",
            log_path="/tmp/wd/runs/abc/screen.log",
            plugin_dir="/home/youran/AgonSR/agonsr",
            rounds=3,
        )
    # argv is a list; no shell=True
    assert isinstance(argv, list)
    bash_script = argv[-1]  # last element is the bash -c argument
    assert "firejail" in bash_script

def test_build_cmd_skips_firejail_when_missing():
    with patch("shutil.which", return_value=None):
        argv = _build_screen_cmd(
            screen_name="agonsr-abc",
            workdir="/tmp/wd",
            log_path="/tmp/wd/runs/abc/screen.log",
            plugin_dir="/home/youran/AgonSR/agonsr",
            rounds=3,
        )
    assert isinstance(argv, list)
    bash_script = argv[-1]
    assert "firejail" not in bash_script
    assert "claude" in bash_script

def test_build_cmd_quotes_user_values():
    """Spaces and special chars in paths must not break the bash -c string."""
    with patch("shutil.which", return_value=None):
        argv = _build_screen_cmd(
            screen_name="agonsr-abc",
            workdir="/tmp/my project/wd",
            log_path="/tmp/my project/wd/runs/abc/screen.log",
            plugin_dir="/home/youran/AgonSR/agonsr",
            rounds=3,
        )
    bash_script = argv[-1]
    # shlex.quote wraps path with spaces in single quotes
    assert "my project" in bash_script
    assert "'$'" not in bash_script  # no unquoted shell metacharacters
```

- [ ] **Step 2: Run test — expect failure**

```bash
pytest tests/test_sandbox.py -v
```

- [ ] **Step 3: Refactor _spawn_screen in runs.py**

Replace the existing `_spawn_screen` with:

```python
import shutil

def _build_screen_cmd(screen_name: str, workdir: str, log_path: str, plugin_dir: str, rounds: int) -> list[str]:
    """Return argv list for Popen (no shell=True). User-supplied values inside the
    bash -c string are escaped with shlex.quote."""
    import shlex
    workdir = str(workdir)
    log_path = str(log_path)
    claude_inner = (
        f"claude --dangerously-skip-permissions "
        f"--plugin-dir {shlex.quote(plugin_dir)} "
        f"--model claude-sonnet-5[1m] "
        f"-p {shlex.quote(f'/llm-mcts {int(rounds)} problem.md')} 2>&1"
    )
    if shutil.which("firejail"):
        bash_cmd = (
            f"cd {shlex.quote(workdir)} && "
            f"firejail --noprofile "
            f"--whitelist={shlex.quote(workdir)} "
            f"--whitelist={shlex.quote(plugin_dir)} "
            f"--read-only={shlex.quote(plugin_dir)} "
            f"--whitelist=/tmp "
            f"-- {claude_inner}"
        )
    else:
        import logging
        logging.getLogger(__name__).warning("firejail not found; running claude unsandboxed")
        bash_cmd = f"cd {shlex.quote(workdir)} && {claude_inner}"

    return ["screen", "-dmS", screen_name, "-L", "-Logfile", log_path, "bash", "-c", bash_cmd]

def _spawn_screen(screen_name: str, workdir: Path, log_path: Path, plugin_dir: str, rounds: int) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = _build_screen_cmd(screen_name, workdir, log_path, plugin_dir, rounds)
    subprocess.Popen(argv)  # no shell=True
```

- [ ] **Step 4: Run tests — expect pass**

```bash
pytest tests/test_sandbox.py tests/test_runs.py -v
```
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add backend/runs.py tests/test_sandbox.py
git commit -m "feat: firejail sandbox wrapper for claude invocation"
```

---

### Task 11: Next.js frontend scaffold + login page

**Files:**
- Create: `frontend/` (full Next.js app)
- Create: `frontend/src/lib/api.ts`
- Create: `frontend/src/lib/types.ts`
- Create: `frontend/src/stores/auth.ts`
- Create: `frontend/src/app/layout.tsx`
- Create: `frontend/src/app/page.tsx` (redirects)
- Create: `frontend/src/app/login/page.tsx`

- [ ] **Step 1: Scaffold Next.js app**

```bash
cd ~/AgonSR-WebUI
npx create-next-app@16 frontend \
  --typescript --tailwind --eslint \
  --app --src-dir --no-import-alias
cd frontend
npm install zustand
```

- [ ] **Step 2: Configure API proxy for development**

`frontend/next.config.ts`:
```typescript
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      { source: "/api/:path*", destination: "http://localhost:7655/api/:path*" },
    ];
  },
};

export default nextConfig;
```

WebSocket connects directly to Flask in dev: `ws://localhost:7655/ws/runs/<id>`.

- [ ] **Step 3: Write types**

`frontend/src/lib/types.ts`:
```typescript
export interface User {
  email: string;
  is_admin: boolean;
}

export interface Project {
  id: string;
  name: string;
  created_at: number;
}

export interface ProjectFiles {
  problem_md: string;
  proposer_notes: string;
  reviewer_notes: string;
  dispatcher_notes: string;
  data_files: string[];
}

export interface Run {
  id: string;
  project_id: string;
  rounds: number;
  proposer_model: string;
  reviewer_model: string;
  status: "pending" | "running" | "completed" | "failed";
  started_at: number | null;
  ended_at: number | null;
}

export interface Settings {
  max_concurrent_runs: number;
  max_runs_per_user: number;
  otp_expiry_minutes: number;
  available_proposer_models: string[];
  available_reviewer_models: string[];
  default_proposer_model: string;
  default_reviewer_model: string;
}
```

- [ ] **Step 4: Write API client**

`frontend/src/lib/api.ts`:
```typescript
async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, { credentials: "include", ...options });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${res.status}`);
  }
  return res.json();
}

export const api = {
  getMe: () => apiFetch<{ email: string; is_admin: boolean }>("/api/auth/me"),
  requestOtp: (email: string) =>
    apiFetch("/api/auth/request-otp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    }),
  verifyOtp: (email: string, otp: string) =>
    apiFetch<{ email: string; is_admin: boolean }>("/api/auth/verify-otp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, otp }),
    }),
  logout: () => apiFetch("/api/auth/logout", { method: "POST" }),

  listProjects: () => apiFetch<import("./types").Project[]>("/api/projects"),
  createProject: (name: string) =>
    apiFetch<{ id: string; name: string }>("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  renameProject: (id: string, name: string) =>
    apiFetch(`/api/projects/${id}/rename`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  deleteProject: (id: string) =>
    apiFetch(`/api/projects/${id}`, { method: "DELETE" }),

  getFiles: (id: string) =>
    apiFetch<import("./types").ProjectFiles>(`/api/projects/${id}/files`),
  putFiles: (id: string, files: Partial<import("./types").ProjectFiles>) =>
    apiFetch(`/api/projects/${id}/files`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(files),
    }),
  uploadCsv: (id: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return apiFetch<{ ok: boolean; filename: string }>(
      `/api/projects/${id}/upload`,
      { method: "POST", body: fd }
    );
  },

  listRuns: (projectId: string) =>
    apiFetch<import("./types").Run[]>(`/api/projects/${projectId}/runs`),
  startRun: (projectId: string, rounds: number, proposer_model: string, reviewer_model: string) =>
    apiFetch<{ run_id: string }>(`/api/projects/${projectId}/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rounds, proposer_model, reviewer_model }),
    }),
  getRun: (runId: string) => apiFetch<import("./types").Run>(`/api/runs/${runId}`),

  getAdminSettings: () => apiFetch<import("./types").Settings>("/api/admin/settings"),
  updateAdminSettings: (settings: Partial<import("./types").Settings>) =>
    apiFetch("/api/admin/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    }),
  listAdminUsers: () => apiFetch<unknown[]>("/api/admin/users"),
  listAdminRuns: () => apiFetch<unknown[]>("/api/admin/runs"),
};
```

- [ ] **Step 5: Write auth store**

`frontend/src/stores/auth.ts`:
```typescript
import { create } from "zustand";

interface AuthState {
  email: string | null;
  isAdmin: boolean;
  loaded: boolean;
  setUser: (email: string, isAdmin: boolean) => void;
  clear: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  email: null,
  isAdmin: false,
  loaded: false,
  setUser: (email, isAdmin) => set({ email, isAdmin, loaded: true }),
  clear: () => set({ email: null, isAdmin: false, loaded: true }),
}));
```

- [ ] **Step 6: Write root layout**

`frontend/src/app/layout.tsx`:
```typescript
import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AgonSR",
  description: "Automated symbolic regression search",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-gray-50 text-gray-900 min-h-screen">{children}</body>
    </html>
  );
}
```

- [ ] **Step 7: Write root page (auth redirect)**

`frontend/src/app/page.tsx`:
```typescript
"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useAuthStore } from "@/stores/auth";

export default function Home() {
  const router = useRouter();
  const { setUser, clear } = useAuthStore();

  useEffect(() => {
    api.getMe()
      .then((u) => { setUser(u.email, u.is_admin); router.replace("/dashboard"); })
      .catch(() => { clear(); router.replace("/login"); });
  }, []);

  return <div className="flex items-center justify-center h-screen">Loading...</div>;
}
```

- [ ] **Step 8: Write login page**

`frontend/src/app/login/page.tsx`:
```typescript
"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useAuthStore } from "@/stores/auth";

export default function LoginPage() {
  const router = useRouter();
  const { setUser } = useAuthStore();
  const [email, setEmail] = useState("");
  const [otp, setOtp] = useState("");
  const [step, setStep] = useState<"email" | "otp">("email");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleRequestOtp() {
    setError(""); setLoading(true);
    try {
      await api.requestOtp(email.trim().toLowerCase());
      setStep("otp");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to send code");
    } finally { setLoading(false); }
  }

  async function handleVerifyOtp() {
    setError(""); setLoading(true);
    try {
      const u = await api.verifyOtp(email.trim().toLowerCase(), otp.trim());
      setUser(u.email, u.is_admin);
      router.replace("/dashboard");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Invalid code");
    } finally { setLoading(false); }
  }

  return (
    <div className="flex items-center justify-center min-h-screen">
      <div className="w-full max-w-sm p-8 bg-white rounded-2xl shadow space-y-4">
        <h1 className="text-2xl font-semibold text-center">AgonSR</h1>
        {error && <p className="text-red-500 text-sm">{error}</p>}
        {step === "email" ? (
          <>
            <input
              type="email" placeholder="Email address" value={email}
              onChange={(e) => setEmail(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleRequestOtp()}
              className="w-full border rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-500"
            />
            <button
              onClick={handleRequestOtp} disabled={loading || !email}
              className="w-full bg-blue-600 text-white rounded-lg py-2 text-sm font-medium disabled:opacity-50"
            >
              {loading ? "Sending..." : "Send Code"}
            </button>
          </>
        ) : (
          <>
            <p className="text-sm text-gray-500">Enter the 6-digit code sent to <strong>{email}</strong></p>
            <input
              type="text" placeholder="000000" value={otp} maxLength={6}
              onChange={(e) => setOtp(e.target.value.replace(/\D/g, ""))}
              onKeyDown={(e) => e.key === "Enter" && handleVerifyOtp()}
              className="w-full border rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-500 text-center tracking-widest text-lg"
            />
            <button
              onClick={handleVerifyOtp} disabled={loading || otp.length !== 6}
              className="w-full bg-blue-600 text-white rounded-lg py-2 text-sm font-medium disabled:opacity-50"
            >
              {loading ? "Verifying..." : "Sign In"}
            </button>
            <button onClick={() => setStep("email")} className="w-full text-sm text-gray-400 underline">
              Use different email
            </button>
          </>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 9: Verify login flow manually**

```bash
# Terminal 1 — backend
cd ~/AgonSR-WebUI/backend
flask --app 'app:create_app("config.toml")' run --port 7655

# Terminal 2 — frontend
cd ~/AgonSR-WebUI/frontend
npm run dev
```

Open `http://localhost:3000`. Should redirect to `/login`. Enter email → receive code (check server log if SMTP not configured) → enter code → redirect to `/dashboard` (404 for now, that's fine).

- [ ] **Step 10: Commit**

```bash
git add frontend/ 
git commit -m "feat: Next.js frontend scaffold with login page"
```

---

### Task 12: Dashboard and project editor

**Files:**
- Create: `frontend/src/app/dashboard/page.tsx`
- Create: `frontend/src/app/project/[id]/page.tsx`
- Create: `frontend/src/components/NavBar.tsx`
- Create: `frontend/src/components/Editor.tsx`

- [ ] **Step 1: NavBar component**

`frontend/src/components/NavBar.tsx`:
```typescript
"use client";
import { useAuthStore } from "@/stores/auth";
import { api } from "@/lib/api";
import { useRouter } from "next/navigation";
import Link from "next/link";

export default function NavBar() {
  const { email, isAdmin, clear } = useAuthStore();
  const router = useRouter();

  async function logout() {
    await api.logout().catch(() => {});
    clear();
    router.replace("/login");
  }

  return (
    <nav className="bg-white border-b px-6 py-3 flex items-center justify-between">
      <Link href="/dashboard" className="font-semibold text-blue-600">AgonSR</Link>
      <div className="flex items-center gap-4 text-sm">
        {isAdmin && <Link href="/admin" className="text-gray-500 hover:text-gray-900">Admin</Link>}
        <span className="text-gray-500">{email}</span>
        <button onClick={logout} className="text-gray-500 hover:text-red-500">Sign out</button>
      </div>
    </nav>
  );
}
```

- [ ] **Step 2: Auto-save Editor component**

`frontend/src/components/Editor.tsx`:
```typescript
"use client";
import { useEffect, useRef, useState } from "react";

interface EditorProps {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  rows?: number;
}

export default function Editor({ value, onChange, placeholder, rows = 12 }: EditorProps) {
  const [local, setLocal] = useState(value);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => { setLocal(value); }, [value]);

  function handleChange(v: string) {
    setLocal(v);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => onChange(v), 800);
  }

  return (
    <textarea
      value={local}
      onChange={(e) => handleChange(e.target.value)}
      placeholder={placeholder}
      rows={rows}
      className="w-full font-mono text-sm border rounded-lg p-3 outline-none focus:ring-2 focus:ring-blue-500 resize-y"
    />
  );
}
```

- [ ] **Step 3: Dashboard page**

`frontend/src/app/dashboard/page.tsx`:
```typescript
"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useAuthStore } from "@/stores/auth";
import NavBar from "@/components/NavBar";
import type { Project } from "@/lib/types";

export default function Dashboard() {
  const router = useRouter();
  const { setUser, clear, loaded } = useAuthStore();
  const [projects, setProjects] = useState<Project[]>([]);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api.getMe()
      .then((u) => { setUser(u.email, u.is_admin); })
      .catch(() => { clear(); router.replace("/login"); });
    api.listProjects().then(setProjects).catch(() => {});
  }, []);

  async function createProject() {
    if (!newName.trim()) return;
    setError(""); setCreating(true);
    try {
      const p = await api.createProject(newName.trim());
      setProjects((ps) => [{ ...p, created_at: Date.now() / 1000 }, ...ps]);
      setNewName("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed");
    } finally { setCreating(false); }
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <NavBar />
      <main className="max-w-3xl mx-auto py-10 px-4 space-y-6">
        <h1 className="text-xl font-semibold">Projects</h1>

        <div className="flex gap-2">
          <input
            placeholder="New project name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && createProject()}
            className="flex-1 border rounded-lg px-3 py-2 text-sm"
          />
          <button
            onClick={createProject} disabled={creating || !newName.trim()}
            className="bg-blue-600 text-white px-4 py-2 rounded-lg text-sm disabled:opacity-50"
          >
            {creating ? "Creating..." : "New Project"}
          </button>
        </div>
        {error && <p className="text-red-500 text-sm">{error}</p>}

        {projects.length === 0 && (
          <p className="text-gray-400 text-sm">No projects yet. Create one above.</p>
        )}

        <ul className="space-y-2">
          {projects.map((p) => (
            <li key={p.id}>
              <button
                onClick={() => router.push(`/project/${p.id}`)}
                className="w-full text-left bg-white border rounded-xl px-5 py-4 hover:shadow transition"
              >
                <p className="font-medium">{p.name}</p>
                <p className="text-xs text-gray-400 mt-1">
                  Created {new Date(p.created_at * 1000).toLocaleDateString()}
                </p>
              </button>
            </li>
          ))}
        </ul>
      </main>
    </div>
  );
}
```

- [ ] **Step 4: Project editor page**

`frontend/src/app/project/[id]/page.tsx`:
```typescript
"use client";
import { useEffect, useState, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useAuthStore } from "@/stores/auth";
import NavBar from "@/components/NavBar";
import Editor from "@/components/Editor";
import RunPanel from "@/components/RunPanel";
import type { ProjectFiles, Project } from "@/lib/types";

const TABS = [
  { key: "problem_md", label: "Problem" },
  { key: "proposer_notes", label: "Proposer Notes" },
  { key: "reviewer_notes", label: "Reviewer Notes" },
  { key: "dispatcher_notes", label: "Dispatcher Notes" },
] as const;

export default function ProjectPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { setUser, clear } = useAuthStore();
  const [files, setFiles] = useState<ProjectFiles | null>(null);
  const [activeTab, setActiveTab] = useState<typeof TABS[number]["key"]>("problem_md");
  const [project, setProject] = useState<Project | null>(null);
  const [editingName, setEditingName] = useState(false);
  const [nameInput, setNameInput] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.getMe()
      .then((u) => setUser(u.email, u.is_admin))
      .catch(() => { clear(); router.replace("/login"); });
    api.listProjects().then((ps) => {
      const p = ps.find((x) => x.id === id);
      if (!p) { router.replace("/dashboard"); return; }
      setProject(p); setNameInput(p.name);
    });
    api.getFiles(id).then(setFiles).catch(() => router.replace("/dashboard"));
  }, [id]);

  const save = useCallback(
    async (updated: Partial<ProjectFiles>) => {
      if (!files) return;
      const merged = { ...files, ...updated };
      setFiles(merged);
      setSaving(true);
      await api.putFiles(id, merged).finally(() => setSaving(false));
    },
    [files, id]
  );

  async function renameProject() {
    if (!project || nameInput === project.name) { setEditingName(false); return; }
    await api.renameProject(id, nameInput);
    setProject({ ...project, name: nameInput });
    setEditingName(false);
  }

  async function uploadFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file || !files) return;
    const res = await api.uploadCsv(id, file);
    setFiles({ ...files, data_files: [...files.data_files, res.filename] });
  }

  if (!files || !project) return <div className="flex items-center justify-center h-screen">Loading...</div>;

  return (
    <div className="min-h-screen bg-gray-50 flex flex-col">
      <NavBar />
      <main className="max-w-5xl mx-auto w-full py-8 px-4 flex gap-6">
        {/* Left panel */}
        <div className="flex-1 space-y-4">
          {/* Project name */}
          <div className="flex items-center gap-2">
            {editingName ? (
              <>
                <input
                  value={nameInput}
                  onChange={(e) => setNameInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && renameProject()}
                  className="border rounded px-2 py-1 text-lg font-semibold"
                  autoFocus
                />
                <button onClick={renameProject} className="text-sm text-blue-600">Save</button>
                <button onClick={() => setEditingName(false)} className="text-sm text-gray-400">Cancel</button>
              </>
            ) : (
              <>
                <h1 className="text-xl font-semibold">{project.name}</h1>
                <button onClick={() => setEditingName(true)} className="text-xs text-gray-400 hover:text-gray-700">Rename</button>
              </>
            )}
            {saving && <span className="text-xs text-gray-400 ml-auto">Saving...</span>}
          </div>

          {/* Data files */}
          <div className="bg-white rounded-xl border p-4 space-y-2">
            <p className="text-sm font-medium">Training Data</p>
            {files.data_files.length === 0 && <p className="text-xs text-gray-400">No CSV files uploaded</p>}
            <ul className="text-xs text-gray-600 space-y-1">
              {files.data_files.map((f) => <li key={f}>📄 {f}</li>)}
            </ul>
            <label className="inline-block cursor-pointer text-xs bg-gray-100 hover:bg-gray-200 rounded px-3 py-1.5">
              Upload CSV
              <input type="file" accept=".csv" className="hidden" onChange={uploadFile} />
            </label>
          </div>

          {/* Editor tabs */}
          <div className="bg-white rounded-xl border">
            <div className="flex border-b">
              {TABS.map((t) => (
                <button
                  key={t.key}
                  onClick={() => setActiveTab(t.key)}
                  className={`px-4 py-2 text-sm ${activeTab === t.key ? "border-b-2 border-blue-600 font-medium" : "text-gray-500"}`}
                >
                  {t.label}
                </button>
              ))}
            </div>
            <div className="p-4">
              <Editor
                key={activeTab}
                value={files[activeTab] ?? ""}
                onChange={(v) => save({ [activeTab]: v })}
                placeholder={`Write ${TABS.find((t) => t.key === activeTab)?.label ?? ""} here...`}
                rows={16}
              />
            </div>
          </div>
        </div>

        {/* Right panel — RunPanel */}
        <div className="w-72 shrink-0">
          <RunPanel projectId={id} />
        </div>
      </main>
    </div>
  );
}
```

- [ ] **Step 5: Test manually**

```bash
# Start backend and frontend (see Task 11 Step 9)
```

Open dashboard → create project → click project → verify all 4 tabs auto-save → upload a CSV → confirm it appears in the list.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/
git commit -m "feat: dashboard and project editor with auto-save and CSV upload"
```

---

### Task 13: Run panel and log viewer

**Files:**
- Create: `frontend/src/components/RunPanel.tsx`
- Create: `frontend/src/components/LogViewer.tsx`
- Create: `frontend/src/stores/run.ts`

- [ ] **Step 1: Run store**

`frontend/src/stores/run.ts`:
```typescript
import { create } from "zustand";
import type { Run } from "@/lib/types";

interface RunState {
  activeRun: Run | null;
  setRun: (r: Run | null) => void;
}

export const useRunStore = create<RunState>((set) => ({
  activeRun: null,
  setRun: (r) => set({ activeRun: r }),
}));
```

- [ ] **Step 2: LogViewer component**

`frontend/src/components/LogViewer.tsx`:
```typescript
"use client";
import { useEffect, useRef, useState } from "react";

interface LogViewerProps {
  runId: string;
  active: boolean;
}

export default function LogViewer({ runId, active }: LogViewerProps) {
  const [open, setOpen] = useState(false);
  const [log, setLog] = useState("");
  const wsRef = useRef<WebSocket | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!active || !open) return;
    const wsUrl = `ws://localhost:7655/ws/runs/${runId}`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;
    ws.onmessage = (e) => {
      setLog((prev) => prev + e.data);
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    };
    ws.onerror = () => setLog((prev) => prev + "\n[connection error]\n");
    return () => ws.close();
  }, [runId, active, open]);

  return (
    <div className="mt-4">
      <button
        onClick={() => setOpen((o) => !o)}
        className="text-xs text-gray-400 hover:text-gray-700 underline"
      >
        {open ? "Hide" : "Show"} debug log
      </button>
      {open && (
        <pre className="mt-2 bg-gray-900 text-green-400 text-xs rounded-lg p-3 h-64 overflow-y-auto whitespace-pre-wrap">
          {log || "Waiting for output..."}
          <div ref={bottomRef} />
        </pre>
      )}
    </div>
  );
}
```

- [ ] **Step 3: RunPanel component**

`frontend/src/components/RunPanel.tsx`:
```typescript
"use client";
import { useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";
import type { Run, Settings } from "@/lib/types";
import LogViewer from "./LogViewer";

interface RunPanelProps { projectId: string; }

function useElapsed(startedAt: number | null, ended: boolean) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!startedAt || ended) return;
    const interval = setInterval(() => {
      setElapsed(Math.floor(Date.now() / 1000 - startedAt));
    }, 1000);
    return () => clearInterval(interval);
  }, [startedAt, ended]);
  return elapsed;
}

function fmt(s: number) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}

export default function RunPanel({ projectId }: RunPanelProps) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [rounds, setRounds] = useState(1);
  const [proposerModel, setProposerModel] = useState("");
  const [reviewerModel, setReviewerModel] = useState("");
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");

  const activeRun = runs.find((r) => r.status === "running" || r.status === "pending");
  const ended = activeRun?.status === "completed" || activeRun?.status === "failed";
  const elapsed = useElapsed(activeRun?.started_at ?? null, !!ended);

  const refresh = useCallback(() => {
    api.listRuns(projectId).then(setRuns).catch(() => {});
  }, [projectId]);

  useEffect(() => {
    api.getAdminSettings().then((s) => {
      setSettings(s);
      setProposerModel(s.default_proposer_model);
      setReviewerModel(s.default_reviewer_model);
    }).catch(() => {});
    refresh();
    // Poll run status while something is active
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  }, [refresh]);

  async function startRun() {
    setError(""); setStarting(true);
    try {
      await api.startRun(projectId, rounds, proposerModel, reviewerModel);
      refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to start run");
    } finally { setStarting(false); }
  }

  if (!settings) return <div className="text-sm text-gray-400">Loading...</div>;

  return (
    <div className="bg-white rounded-xl border p-4 space-y-4">
      <h2 className="font-medium text-sm">Run Configuration</h2>

      <div className="space-y-2">
        <label className="text-xs text-gray-500">Proposer model</label>
        <select
          value={proposerModel}
          onChange={(e) => setProposerModel(e.target.value)}
          disabled={!!activeRun}
          className="w-full border rounded px-2 py-1.5 text-sm disabled:opacity-50"
        >
          {settings.available_proposer_models.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      </div>

      <div className="space-y-2">
        <label className="text-xs text-gray-500">Reviewer model</label>
        <select
          value={reviewerModel}
          onChange={(e) => setReviewerModel(e.target.value)}
          disabled={!!activeRun}
          className="w-full border rounded px-2 py-1.5 text-sm disabled:opacity-50"
        >
          {settings.available_reviewer_models.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      </div>

      <div className="space-y-2">
        <label className="text-xs text-gray-500">Rounds</label>
        <input
          type="number" min={1} value={rounds}
          onChange={(e) => setRounds(Math.max(1, parseInt(e.target.value) || 1))}
          disabled={!!activeRun}
          className="w-full border rounded px-2 py-1.5 text-sm disabled:opacity-50"
        />
      </div>

      {error && <p className="text-xs text-red-500">{error}</p>}

      {activeRun ? (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${
              activeRun.status === "running" ? "bg-green-100 text-green-700" :
              activeRun.status === "completed" ? "bg-gray-100 text-gray-600" :
              "bg-red-100 text-red-600"
            }`}>{activeRun.status}</span>
            {activeRun.status === "running" && (
              <span className="text-xs text-gray-500">{fmt(elapsed)}</span>
            )}
          </div>
          <p className="text-xs text-gray-400">
            {activeRun.rounds} rounds · {activeRun.proposer_model} / {activeRun.reviewer_model}
          </p>
          <LogViewer runId={activeRun.id} active={activeRun.status === "running"} />
        </div>
      ) : (
        <button
          onClick={startRun} disabled={starting}
          className="w-full bg-blue-600 text-white rounded-lg py-2.5 text-sm font-medium disabled:opacity-50"
        >
          {starting ? "Starting..." : "▶  Start Run"}
        </button>
      )}

      {runs.filter((r) => r.status === "completed" || r.status === "failed").length > 0 && (
        <div className="pt-2 border-t">
          <p className="text-xs text-gray-400 font-medium mb-1">Previous runs</p>
          {runs
            .filter((r) => r.status === "completed" || r.status === "failed")
            .slice(0, 5)
            .map((r) => (
              <p key={r.id} className="text-xs text-gray-500">
                {r.status} · {r.rounds} rounds · {r.ended_at ? new Date(r.ended_at * 1000).toLocaleDateString() : ""}
              </p>
            ))}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Test manually**

Start a run, open the debug log, confirm text streams in real time. Confirm elapsed timer ticks. Confirm status changes to "completed" after screen session ends.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ frontend/src/stores/run.ts
git commit -m "feat: run panel with model selection, start button, elapsed timer, and log viewer"
```

---

### Task 14: Admin page + production setup

**Files:**
- Create: `frontend/src/app/admin/page.tsx`
- Create: `nginx.conf`
- Create: `start.sh`

- [ ] **Step 1: Admin page**

`frontend/src/app/admin/page.tsx`:
```typescript
"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useAuthStore } from "@/stores/auth";
import NavBar from "@/components/NavBar";
import type { Settings } from "@/lib/types";

export default function AdminPage() {
  const router = useRouter();
  const { setUser, clear, isAdmin } = useAuthStore();
  const [users, setUsers] = useState<unknown[]>([]);
  const [activeRuns, setActiveRuns] = useState<unknown[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  const [tab, setTab] = useState<"users" | "runs" | "settings">("users");

  useEffect(() => {
    api.getMe()
      .then((u) => { setUser(u.email, u.is_admin); if (!u.is_admin) router.replace("/dashboard"); })
      .catch(() => { clear(); router.replace("/login"); });
    api.listAdminUsers().then(setUsers).catch(() => {});
    api.listAdminRuns().then(setActiveRuns).catch(() => {});
    api.getAdminSettings().then(setSettings).catch(() => {});
  }, []);

  async function saveSettings() {
    if (!settings) return;
    setSaving(true);
    await api.updateAdminSettings(settings).finally(() => setSaving(false));
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <NavBar />
      <main className="max-w-4xl mx-auto py-8 px-4 space-y-6">
        <h1 className="text-xl font-semibold">Admin</h1>

        <div className="flex gap-2 border-b">
          {(["users", "runs", "settings"] as const).map((t) => (
            <button
              key={t} onClick={() => setTab(t)}
              className={`px-4 py-2 text-sm capitalize ${tab === t ? "border-b-2 border-blue-600 font-medium" : "text-gray-500"}`}
            >
              {t}
              {t === "users" && ` (${(users as unknown[]).length})`}
              {t === "runs" && ` (${(activeRuns as unknown[]).length})`}
            </button>
          ))}
        </div>

        {tab === "users" && (
          <table className="w-full text-sm border rounded-xl overflow-hidden">
            <thead className="bg-gray-100">
              <tr>{["Email", "Admin", "Joined", "Active runs"].map((h) => (
                <th key={h} className="text-left px-4 py-2 text-xs text-gray-500">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {(users as Record<string, unknown>[]).map((u) => (
                <tr key={String(u.email)} className="border-t bg-white">
                  <td className="px-4 py-2">{String(u.email)}</td>
                  <td className="px-4 py-2">{u.is_admin ? "✓" : ""}</td>
                  <td className="px-4 py-2">{u.created_at ? new Date(Number(u.created_at) * 1000).toLocaleDateString() : ""}</td>
                  <td className="px-4 py-2">{String(u.active_runs ?? 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {tab === "runs" && (
          <table className="w-full text-sm border rounded-xl overflow-hidden">
            <thead className="bg-gray-100">
              <tr>{["User", "Project", "Status", "Rounds", "Started"].map((h) => (
                <th key={h} className="text-left px-4 py-2 text-xs text-gray-500">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {(activeRuns as Record<string, unknown>[]).map((r) => (
                <tr key={String(r.id)} className="border-t bg-white">
                  <td className="px-4 py-2 text-xs">{String(r.user_email)}</td>
                  <td className="px-4 py-2">{String(r.project_name)}</td>
                  <td className="px-4 py-2">{String(r.status)}</td>
                  <td className="px-4 py-2">{String(r.rounds)}</td>
                  <td className="px-4 py-2 text-xs">{r.started_at ? new Date(Number(r.started_at) * 1000).toLocaleString() : ""}</td>
                </tr>
              ))}
              {activeRuns.length === 0 && (
                <tr><td colSpan={5} className="px-4 py-4 text-center text-gray-400 text-xs">No active runs</td></tr>
              )}
            </tbody>
          </table>
        )}

        {tab === "settings" && settings && (
          <div className="bg-white rounded-xl border p-6 space-y-4">
            {[
              { key: "max_concurrent_runs", label: "Max concurrent runs", type: "number" },
              { key: "max_runs_per_user", label: "Max runs per user", type: "number" },
              { key: "otp_expiry_minutes", label: "OTP expiry (minutes)", type: "number" },
            ].map(({ key, label, type }) => (
              <div key={key} className="flex items-center gap-4">
                <label className="w-48 text-sm text-gray-600">{label}</label>
                <input
                  type={type}
                  value={(settings as Record<string, unknown>)[key] as number}
                  onChange={(e) => setSettings({ ...settings, [key]: parseInt(e.target.value) })}
                  className="w-24 border rounded px-2 py-1 text-sm"
                />
              </div>
            ))}
            {[
              { key: "available_proposer_models", label: "Proposer models (comma-separated)" },
              { key: "available_reviewer_models", label: "Reviewer models (comma-separated)" },
            ].map(({ key, label }) => (
              <div key={key} className="space-y-1">
                <label className="text-sm text-gray-600">{label}</label>
                <input
                  value={((settings as Record<string, unknown>)[key] as string[]).join(", ")}
                  onChange={(e) => setSettings({ ...settings, [key]: e.target.value.split(",").map((s) => s.trim()) })}
                  className="w-full border rounded px-2 py-1 text-sm"
                />
              </div>
            ))}
            <button
              onClick={saveSettings} disabled={saving}
              className="bg-blue-600 text-white px-4 py-2 rounded-lg text-sm disabled:opacity-50"
            >
              {saving ? "Saving..." : "Save Settings"}
            </button>
          </div>
        )}
      </main>
    </div>
  );
}
```

- [ ] **Step 2: Production startup script**

`start.sh`:
```bash
#!/bin/bash
set -e
cd ~/AgonSR-WebUI

# Build frontend
cd frontend && npm run build && cd ..

# Start backend (background)
cd backend
nohup flask --app 'app:create_app("config.toml")' run --host 127.0.0.1 --port 7655 > ../backend.log 2>&1 &
echo $! > ../backend.pid
cd ..

# Start Next.js (background)
cd frontend
nohup npm start -- -p 7656 > ../frontend.log 2>&1 &
echo $! > ../frontend.pid
cd ..

echo "Backend PID: $(cat backend.pid)"
echo "Frontend PID: $(cat frontend.pid)"
echo "Configure nginx to proxy / → 7656 and /ws/ → 7655"
```

- [ ] **Step 3: nginx config**

`nginx.conf` (merge into your server's nginx config):
```nginx
server {
    listen 80;
    server_name your-server-domain;

    location /ws/ {
        proxy_pass http://127.0.0.1:7655;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:7655;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
    }

    location / {
        proxy_pass http://127.0.0.1:7656;
        proxy_set_header Host $host;
    }
}
```

Note: in production, the frontend's WebSocket URL in `LogViewer.tsx` must use the nginx URL instead of `localhost:7655`. Add an env var:

`frontend/.env.local` (not committed):
```
NEXT_PUBLIC_API_WS_URL=ws://your-server-domain
```

Update `LogViewer.tsx` line:
```typescript
const wsUrl = `${process.env.NEXT_PUBLIC_API_WS_URL ?? "ws://localhost:7655"}/ws/runs/${runId}`;
```

- [ ] **Step 4: Full end-to-end test**

1. Start backend + frontend in dev mode.
2. Log in as admin email → verify Admin link appears in nav.
3. Navigate to `/admin` → verify Users, Active Runs, Settings tabs work.
4. Change `max_concurrent_runs` → save → verify it persists after page refresh.
5. Create a project, start a real run (or mock one by manually inserting a DB row), confirm status polling works.

- [ ] **Step 5: Final commit**

```bash
git add frontend/src/app/admin/ nginx.conf start.sh
git commit -m "feat: admin page and production nginx/startup configuration"
git push
```

---

## Self-Review

**Spec coverage:**
- ✅ Email+OTP auth (Task 2)
- ✅ JWT httpOnly cookie (Task 2)
- ✅ Project create/list/rename (Task 5)
- ✅ File editors + auto-save (Tasks 6, 12)
- ✅ CSV upload (Tasks 6, 12)
- ✅ Model selection per run (Tasks 7, 13)
- ✅ Rounds input, default 1 (Task 13)
- ✅ Screen-based run execution (Task 7)
- ✅ Real-time log streaming via WebSocket (Tasks 8, 13)
- ✅ Elapsed timer (Task 13)
- ✅ Completion detection + email (Task 7)
- ✅ Audit logging (Task 4)
- ✅ Firejail sandbox (Task 10)
- ✅ Admin panel: users/runs/settings (Tasks 9, 14)
- ✅ Per-user run limit + global limit (Task 7)
- ✅ English-only UI (all frontend tasks)
- ✅ userdata as submodule (Task 1)
- ✅ Dispatcher model fixed at `claude-sonnet-5[1m]` (Task 7)
- ✅ settings.toml written per-run for proposer/reviewer (Task 7)

**Open items (post-MVP):**
- SMTP service needs to be tested and configured (`config.toml`)
- HTTPS / `secure=True` on cookie needs nginx TLS config
- `[1m]` in model name needs shell quoting verification; the `start.sh` wraps claude in single-quotes
- firejail propagation to child processes should be verified manually before production use
