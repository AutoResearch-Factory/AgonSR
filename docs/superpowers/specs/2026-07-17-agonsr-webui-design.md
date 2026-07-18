# AgonSR WebUI — Design Spec

**Date:** 2026-07-17

---

## 1. Repositories

| Repo | Visibility | Purpose |
|------|-----------|---------|
| `AutoResearch-Factory/AgonSR` | public | Plugin prompts, commands, scripts |
| `AutoResearch-Factory/AgonSR-WebUI` | public | Web application (backend + frontend) |
| `AutoResearch-Factory/AgonSR-UserData` | **private** | Per-user project files (submodule of WebUI) |

Server layout:
```
~/AgonSR/           → AutoResearch-Factory/AgonSR
~/AgonSR-WebUI/     → AutoResearch-Factory/AgonSR-WebUI
  └── userdata/     → submodule → AutoResearch-Factory/AgonSR-UserData
```

---

## 2. Tech Stack

- **Backend:** Python + Flask + flask-sock + SQLite
- **Frontend:** Next.js 16, React 19, Tailwind v4, TypeScript, Zustand
- **Session:** JWT in httpOnly cookie (24h)
- **Real-time:** WebSocket (flask-sock) tailing screen log

Matches the SibylSystem stack exactly.

---

## 3. Repository Structure (AgonSR-WebUI)

```
AgonSR-WebUI/
  backend/
    app.py           ← Flask app factory, blueprints
    auth.py          ← OTP generation, JWT issue/verify
    projects.py      ← project CRUD endpoints
    runs.py          ← run start/stop, status polling thread
    admin.py         ← admin-only endpoints
    ws.py            ← WebSocket log-tail endpoint
    db.py            ← SQLite schema + helpers
    email_client.py  ← send OTP + completion notification
    sandbox.py       ← firejail wrapper around claude invocation
    audit.py         ← audit log middleware
    config.py        ← load config.toml
    config.example.toml
    requirements.txt
  frontend/
    src/
      app/
        page.tsx              ← login page
        dashboard/page.tsx    ← project list
        project/[id]/page.tsx ← editor + run panel
        admin/page.tsx        ← admin panel
      components/
        Editor.tsx            ← auto-save textarea
        RunPanel.tsx          ← model select, rounds, start button, status
        LogViewer.tsx         ← collapsible WebSocket log
        ProjectList.tsx
      stores/
        project.ts            ← Zustand store
        run.ts
      lib/
        api.ts                ← typed fetch wrappers
        types.ts
    package.json
  userdata/                   ← git submodule (AutoResearch-Factory/AgonSR-UserData)
  README.md
```

---

## 4. Database Schema (SQLite)

```sql
CREATE TABLE users (
  email       TEXT PRIMARY KEY,
  is_admin    INTEGER NOT NULL DEFAULT 0,
  otp_hash    TEXT,
  otp_expiry  REAL,
  created_at  REAL NOT NULL
);

CREATE TABLE projects (
  id          TEXT PRIMARY KEY,   -- uuid4
  user_email  TEXT NOT NULL REFERENCES users(email),
  name        TEXT NOT NULL,      -- also used as directory name
  created_at  REAL NOT NULL
);

CREATE TABLE runs (
  id              TEXT PRIMARY KEY,  -- uuid4
  project_id      TEXT NOT NULL REFERENCES projects(id),
  rounds          INTEGER NOT NULL,
  proposer_model  TEXT NOT NULL,
  reviewer_model  TEXT NOT NULL,
  screen_name     TEXT,              -- screen session name
  log_path        TEXT,              -- absolute path to screen.log
  status          TEXT NOT NULL DEFAULT 'pending',
                                     -- pending | running | completed | failed
  started_at      REAL,
  ended_at        REAL
);

CREATE TABLE audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp   REAL NOT NULL,
  user_email  TEXT,
  ip          TEXT,
  action      TEXT NOT NULL,
  details     TEXT                   -- JSON blob
);

CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL                -- JSON
);
-- Initial rows:
-- max_concurrent_runs = 3
-- max_runs_per_user   = 1
-- otp_expiry_minutes  = 10
-- available_proposer_models = ["codex","claude","claude-ds","claude-codex"]
-- available_reviewer_models = ["codex","claude","claude-ds","claude-codex"]
-- default_proposer_model = "codex"
-- default_reviewer_model = "codex"
```

---

## 5. Authentication

**Email + OTP, no password.**

Flow:
1. `POST /api/auth/request-otp {email}` — generate 6-digit OTP, store bcrypt hash + expiry in `users`, send email.
2. `POST /api/auth/verify-otp {email, otp}` — verify hash and expiry → issue JWT in httpOnly cookie.
3. JWT payload: `{email, is_admin, exp}`. All protected routes verify JWT via decorator.
4. Admin emails are listed in `config.toml` (not DB) and set `is_admin=1` on first login.

OTP email is re-generatable; requesting a new OTP invalidates the old one.

---

## 6. Project Management

**Directory:** `userdata/<email>/<project_name>/`

Files created on project creation:
```
problem.md      ← empty, user edits in browser
IGNOREME.md     ← template with all three sections
data/           ← directory for CSV uploads
runs/           ← created by the plugin on first run
```

`IGNOREME.md` template:
```markdown
## Notes to ansatz-proposer

## Notes to ansatz-reviewer

## Notes to dispatcher

```

**API endpoints:**
- `GET  /api/projects` — list user's projects
- `POST /api/projects {name}` — create project + directory
- `PUT  /api/projects/<id>/rename {name}` — rename directory + DB record
- `GET  /api/projects/<id>/files` — return problem.md + IGNOREME.md sections
- `PUT  /api/projects/<id>/files {problem_md, proposer_notes, reviewer_notes, dispatcher_notes}` — write files (auto-save calls this)
- `POST /api/projects/<id>/upload` — upload CSV to `data/`

Path-traversal guard: resolve all paths and assert they remain under `userdata/<email>/`.

---

## 7. Run Execution

### Pre-flight checks
1. User has no run with `status=running` (enforced per-user limit from settings).
2. Global `COUNT(status='running') < max_concurrent_runs`.

### Launch sequence
1. Write `userdata/<email>/<project>/settings.toml`:
   ```toml
   ansatz-proposer-model = "<proposer_model>"
   ansatz-reviewer-model = "<reviewer_model>"
   ```
2. Create run record in DB (`status=pending`).
3. Create log directory: `userdata/<email>/<project>/runs/<run_id>/`.
   Note: the plugin's `mcts.py init` will separately create `runs/llm-mcts_YYMMDD_HHMM/` for its own artifacts. These naming schemes (`<uuid4>/` vs `llm-mcts_*/`) do not conflict.
4. Spawn screen session:
   ```bash
   screen -dmS agonsr-<run_id> \
     -L -Logfile userdata/<email>/<project>/runs/<run_id>/screen.log \
     bash -c 'cd userdata/<email>/<project> && \
       claude --dangerously-skip-permissions \
         --plugin-dir ~/AgonSR/agonsr \
         --model claude-sonnet-5[1m] \
         -p "/llm-mcts <rounds> problem.md" 2>&1'
   ```
5. Update run `status=running`, set `started_at`.

### Completion detection
Background thread (daemon, started at app init) polls every 10 seconds:
```python
screen -ls | grep agonsr-<run_id>
```
When the session disappears → update `status=completed`, set `ended_at`, send completion email.
If the process exits non-zero (detectable via exit code written to a wrapper script) → `status=failed`.

### Dispatcher model
Fixed at `claude-sonnet-5[1m]` per README. Not exposed to users or admin settings.

---

## 8. Real-time Log Streaming

**Endpoint:** `GET /ws/runs/<run_id>` (WebSocket)

Server-side:
- Open `screen.log`, seek to end, then `read()` in a loop with 0.5s sleep between empty reads.
- Push each new chunk as a plain-text WebSocket frame.
- Close when run `status` is no longer `running` and file has no new data for 5s.

Frontend (`LogViewer.tsx`):
- Collapsible `<pre>` block, default collapsed.
- Appends incoming text; auto-scrolls if already at bottom.
- Elapsed timer: start at `run.started_at`, stop when status is `completed`/`failed`.

---

## 9. Security & Sandboxing

### Linux-level (firejail)
Wrap the claude invocation:
```bash
firejail --noprofile \
  --whitelist=<workdir> \
  --whitelist=~/AgonSR/agonsr \
  --read-only=~/AgonSR/agonsr \
  --whitelist=/tmp/$USER \
  -- claude --dangerously-skip-permissions ...
```
This constrains claude and all child processes (proposer/reviewer subagents) to:
- Read/write only `<workdir>` and `/tmp/$USER`
- Read-only access to the plugin directory

**Open question:** Verify that firejail's filesystem whitelist propagates correctly to claude's bash-spawned subprocesses. Test required before implementation.

### Claude-level
Place `.claude/settings.json` in workdir at run start:
```json
{"permissions": {"allow": ["Bash(*)", "Read(*)", "Edit(*)", "Write(*)"],
                 "deny": ["Bash(rm -rf *)", "Bash(curl *)", "Bash(wget *)"]}}
```
Second layer; firejail is the primary trust boundary.

### Audit logging
Middleware logs every request:
```python
{timestamp, user_email, ip, method, path, status_code, body_summary}
```
Sensitive actions (run start, file edit, csv upload) also log full details.

---

## 10. Admin Panel

**Access:** `is_admin=True` in JWT. Admin emails set in `config.toml`.

**Displays:**
- User table: email, join date, total runs, currently running
- Active runs table: user, project, started_at, elapsed, rounds
- Settings form (reads/writes `settings` table):
  - Max concurrent runs
  - Max runs per user
  - OTP expiry (minutes)
  - Available proposer models (comma-separated list)
  - Available reviewer models (comma-separated list)

---

## 11. UI Pages

### Login (`/`)
- Email input → "Send Code" button
- OTP input → "Sign In" button
- English only

### Dashboard (`/dashboard`)
- Project cards: name, last run date, last best score (if available)
- "New Project" button → name input modal
- Click project → project page

### Project Page (`/project/<id>`)
- **Header:** project name (click to rename inline)
- **File panel (left or top):** CSV upload drop zone, list of uploaded files
- **Editor tabs:**
  - `problem.md` — full-width textarea, auto-save on debounce (1s)
  - `Proposer Notes` — textarea
  - `Reviewer Notes` — textarea
  - `Dispatcher Notes` — textarea
- **Run Panel (right sidebar or bottom):**
  - Proposer model dropdown (from settings)
  - Reviewer model dropdown (from settings)
  - Rounds input (number, default 1, min 1)
  - Start button (disabled if run active or global queue full)
  - Active run: elapsed timer, status badge
  - "Debug log" collapsible → `<pre>` with WebSocket stream

### Admin (`/admin`)
- Tabs: Users | Active Runs | Settings

---

## 12. Email

SMTP config in `config.toml` (to be determined: local postfix / Gmail / SendGrid).
Two templates:
- **OTP:** "Your AgonSR verification code is: `123456`. Valid for 10 minutes."
- **Completion:** "Your run on project `<name>` has finished. Rounds: N. Check results at <URL>."

---

## 13. Configuration File

`backend/config.toml` (not committed; `config.example.toml` committed):
```toml
[server]
host = "127.0.0.1"
port = 7655
secret_key = "<random>"

[paths]
plugin_dir = "/home/youran/AgonSR/agonsr"
userdata_dir = "/home/youran/AgonSR-WebUI/userdata"
db_path = "/home/youran/AgonSR-WebUI/backend/agonsr.db"

[email]
smtp_host = ""
smtp_port = 587
smtp_user = ""
smtp_password = ""
from_address = ""

[admin]
emails = ["guojx@stanford.edu"]
```

---

## 14. Open Questions (pre-implementation)

1. **firejail + claude subprocess propagation:** Confirm that subagents spawned via bash inside firejail are also sandboxed correctly. If not, evaluate `bubblewrap` as alternative.
2. **SMTP:** Determine which mail service works on umdoffice.
3. **screen vs tmux:** SibylSystem uses tmux. If screen causes issues (e.g., log format, session detection), switch to tmux; interface is similar.
