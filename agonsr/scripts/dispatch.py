#!/usr/bin/env python3
"""Deterministic dispatcher for AgonSR — a headless replacement for /llm-mcts.

Runs the MCTS ansatz-search loop without an LLM dispatcher: init-or-resume the
search state, then for each round call mcts.py next, dispatch the proposer and
reviewer CLI subagents, parse the review score, and call mcts.py update.

Semantics:
- One search per workdir (state lives in <workdir>/search). If the state
  exists the script resumes it; a fresh search means a fresh workdir.
- Fail fast: contract violations (missing ansatz.md, unparsable score) write
  search/dispatch_status.json and exit non-zero. Restarting the script cleans
  the dangling candidate into _dirty_* and redoes it — the product's retry
  loop, no internal retries.
- Live logs: codex subagents stream stdout; claude-family subagents get a
  preset --session-id and their transcript (~/.claude/projects/*/<sid>.jsonl)
  is tailed while they run.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SEARCH_DIRNAME = "search"
CODEX_MODEL = "gpt-5.6-sol"

# Set by the WebUI when it starts a run. With them the subagent authenticates
# through a proxy that holds the real credentials outside the sandbox; without
# them we are being run by hand, so the operator's own credentials are mounted
# read-only instead. Either way the filesystem confinement is identical.
PROXY_URL_ENV = "AGONSR_PROXY_URL"
RUN_TOKEN_ENV = "AGONSR_RUN_TOKEN"

CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
CLAUDE_SETTINGS = Path.home() / ".claude.json"
CODEX_CREDENTIALS = Path.home() / ".codex" / "auth.json"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"

# Standalone mode hands the CLIs their own config trees rather than a synthetic
# minimum, because both reference absolute host paths from inside their config
# and a hand-built substitute breaks the moment either adds another one.
STANDALONE_HOME_MOUNTS = (Path(".claude"), Path(".codex"), Path(".claude.json"))

PROPOSER_TEMPLATE = (
    "CLAUDE_PLUGIN_ROOT: {plugin}, PROBLEM_PATH: {problem}, "
    "ANCESTOR_REPORTS: {ancestors}, WORKDIR: {workdir}, "
    "Special notes from user: {notes}"
)
REVIEWER_TEMPLATE = (
    "CLAUDE_PLUGIN_ROOT: {plugin}, PROBLEM_PATH: {problem}, "
    "WORKDIR: {workdir}, Special notes from user: {notes}"
)

IGNOREME_HEADERS = {
    "proposer": "## Notes to ansatz-proposer",
    "reviewer": "## Notes to ansatz-reviewer",
    "dispatcher": "## Notes to dispatcher",
}


def log(role: str, msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp} {role}] {msg}", flush=True)


def _now_stamp() -> str:
    return time.strftime("%y%m%d_%H%M")


# ---------------------------------------------------------------- IGNOREME

def parse_ignoreme(workdir: Path) -> dict[str, str]:
    notes = {k: "" for k in IGNOREME_HEADERS}
    path = workdir / "IGNOREME.md"
    if not path.exists():
        return notes
    text = path.read_text(encoding="utf-8", errors="replace")
    headers = list(IGNOREME_HEADERS.values())
    for key, header in IGNOREME_HEADERS.items():
        others = [re.escape(h) for h in headers if h != header]
        stop = "(?=" + "|".join(others + [r"\Z"]) + ")"
        m = re.search(re.escape(header) + r"\s*(.*?)" + stop, text, re.DOTALL)
        notes[key] = m.group(1).strip() if m else ""
    return notes


# ---------------------------------------------------------------- mcts glue

def mcts(workdir: Path, *args: str) -> str:
    script = PLUGIN_ROOT / "scripts" / "mcts.py"
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=workdir, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        fail_hard(workdir, "mcts_error",
                  f"mcts.py {args[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def ancestor_paths(ancestors: str) -> list[Path]:
    """The ansatz files listed by `mcts.py next`, and only those.

    Non-ancestors are deliberately absent: the proposer prompt already asks the
    agent not to read them, and the sandbox mounts nothing that is not here, so
    the request is backed by the kernel rather than resting on good behaviour.
    """
    if not ancestors or ancestors.strip() == "none":
        return []
    # "- ancestor 1 (father): /path/to/0001/ansatz.md". Project names may
    # contain spaces, so the path runs to end of line rather than to whitespace.
    return [Path(m) for m in re.findall(r"^-\s*[^:]*:\s*(.+?/ansatz\.md)\s*$",
                                        ancestors, re.MULTILINE)]


def parse_next(out: str) -> tuple[str, Path, str]:
    cid = re.search(r"CANDIDATE_ID:\s*(\S+)", out)
    wd = re.search(r"WORKDIR:\s*(\S+)", out)
    if not cid or not wd:
        raise ValueError(f"unparsable mcts.py next output:\n{out}")
    ancestors = out.split("ANCESTOR_REPORTS:", 1)[1].strip() if "ANCESTOR_REPORTS:" in out else "none"
    return cid.group(1), Path(wd.group(1)), ancestors


def clean_dangling(candidate_dir: Path) -> None:
    """Move leftovers of an interrupted attempt into _dirty_<ts>/ (plugin convention)."""
    if not candidate_dir.exists():
        return
    leftovers = [p for p in candidate_dir.iterdir() if not p.name.startswith("_dirty_")]
    if not leftovers:
        return
    dirty = candidate_dir / f"_dirty_{_now_stamp()}"
    dirty.mkdir(exist_ok=True)
    for p in leftovers:
        shutil.move(str(p), str(dirty / p.name))
    log("dispatch", f"moved {len(leftovers)} leftover item(s) into {dirty.name}/")


# ---------------------------------------------------------------- status

def write_status(workdir: Path, **fields) -> None:
    search = workdir / SEARCH_DIRNAME
    search.mkdir(exist_ok=True)
    fields.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
    (search / "dispatch_status.json").write_text(
        json.dumps(fields, indent=2), encoding="utf-8"
    )


def fail_hard(workdir: Path, reason: str, detail: str, **ctx) -> None:
    log("dispatch", f"FAILED: {reason} — {detail}")
    write_status(workdir, status="failed", reason=reason, detail=detail, **ctx)
    sys.exit(1)


# ---------------------------------------------------------------- subagents

def _transcript_roots(config_dir: Path | None = None) -> list[Path]:
    roots = []
    if config_dir:
        roots.append(Path(config_dir) / "projects")
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        roots.append(Path(cfg) / "projects")
    roots.append(Path.home() / ".claude" / "projects")
    return roots


def _find_transcript(session_id: str, config_dir: Path | None = None) -> Path | None:
    for root in _transcript_roots(config_dir):
        hits = list(root.glob(f"*/{session_id}.jsonl"))
        if hits:
            return hits[0]
    return None


def _render_event(role: str, line: str) -> None:
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return
    t = d.get("type")
    if t == "assistant":
        for c in d.get("message", {}).get("content", []):
            kind = c.get("type")
            if kind == "text" and c.get("text", "").strip():
                text = " ".join(c["text"].split())
                log(role, f"says: {text[:200]}")
            elif kind == "tool_use":
                name = c.get("name", "?")
                inp = c.get("input", {})
                if name == "Bash" and isinstance(inp, dict) and "command" in inp:
                    detail = inp["command"]
                else:
                    detail = json.dumps(inp, ensure_ascii=False)
                detail = " ".join(str(detail).split())
                log(role, f"tool {name}: {detail[:160]}")


def _render_codex_event(role: str, line: str) -> str | None:
    """Render one `codex exec --json` event, and return a session id if it
    carries one.

    Same shape as _render_event above, and the same reason: only the events
    named here reach the log. Streaming codex's plain stdout instead put its
    startup banner and the whole prompt — the task line and the agent
    definition it was piped, echoed back as a <stdin> block — in front of
    whoever opens the run log. Picking events out means anything new codex
    decides to print stays out by default rather than leaking by default.
    """
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    kind = d.get("type")
    if kind == "thread.started":
        return d.get("thread_id")
    if kind == "error":
        log(role, f"error: {str(d.get('message', d))[:200]}")
        return None
    if kind != "item.completed":
        return None

    item = d.get("item") or {}
    itype = item.get("type")
    # Labels follow codex's own names for these events, so the log reads the
    # way running codex by hand does.
    if itype == "agent_message":
        text = " ".join(str(item.get("text", "")).split())
        if text:
            log(role, f"message: {text[:200]}")
    elif itype == "command_execution":
        command = " ".join(str(item.get("command", "")).split())
        code = item.get("exit_code")
        log(role, f"exec: {command[:160]}" + (f" (exit={code})" if code else ""))
        output = " ".join(str(item.get("aggregated_output", "")).split())
        if output:
            log(role, f"  → {output[:160]}")
    return None


class TranscriptTail:
    """Streams a claude session transcript into the log as it grows."""

    def __init__(self, role: str, session_id: str, config_dir: Path | None = None):
        self.role = role
        self.session_id = session_id
        self.config_dir = config_dir
        self.path: Path | None = None
        self.offset = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def poll(self) -> None:
        if self.path is None:
            self.path = _find_transcript(self.session_id, self.config_dir)
            if self.path is None:
                return
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self.offset)
                chunk = f.read()
                self.offset = f.tell()
        except OSError:
            return
        for line in chunk.splitlines():
            if line.strip():
                _render_event(self.role, line)

    def _loop(self) -> None:
        while not self.stop.is_set():
            self.poll()
            self.stop.wait(2)

    def __enter__(self) -> "TranscriptTail":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.thread.join(timeout=5)
        self.poll()  # final drain — events between last poll and process exit


def _scratch_root() -> str | None:
    """Somewhere to put per-invocation scratch that is not /tmp.

    codex refuses to create its PATH aliases when CODEX_HOME sits under /tmp,
    so prefer the per-user runtime directory and fall back only if it is gone.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        root = Path(runtime) / "agonsr"
        root.mkdir(parents=True, exist_ok=True)
        return str(root)
    return None


class _Sandbox:
    """One bwrap namespace, alive for exactly one subagent invocation."""

    def __init__(self, argv: list[str], exe: Path, scratch: Path, home: Path):
        self.argv = argv
        self.exe = exe
        self.scratch = scratch
        self.home = home

    @property
    def claude_config_dir(self) -> Path:
        return self.home / ".claude"

    def wrap(self, cmd: list[str]) -> list[str]:
        """Prefix the command and point it at the executable's bound path."""
        return self.argv + [str(self.exe)] + cmd[1:]


@contextlib.contextmanager
def _sandboxed(binary: str, cwd: Path, ro_paths: list[Path]):
    """Confine one subagent to `cwd`, with `ro_paths` readable and nothing else.

    The scratch home exists so the CLI has somewhere to write session logs: the
    host can still tail them (that is how live logging works) and the directory
    is deleted when the call returns, so repeated runs cannot silt up the box.
    """
    sandbox.require()
    exe = Path(shutil.which(binary) or binary).resolve()
    scratch = Path(tempfile.mkdtemp(prefix="agonsr-sandbox-", dir=_scratch_root()))
    try:
        proxy = os.environ.get(PROXY_URL_ENV)
        token = os.environ.get(RUN_TOKEN_ENV)
        served = bool(proxy and token)
        home = scratch if served else Path.home()

        # Both CLIs refuse to start when their config directory is missing, and
        # a served run's home is a scratch directory that starts out empty.
        for name in (".claude", ".codex"):
            (home / name).mkdir(parents=True, exist_ok=True)

        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(home),
            "TMPDIR": "/tmp",
            "USER": os.environ.get("USER", "agonsr"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": "dumb",
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "CODEX_HOME": str(home / ".codex"),
        }
        mounts = [PLUGIN_ROOT, exe, *ro_paths]
        writable = [scratch]

        if served:
            # Started by the WebUI. The real credentials never enter the
            # namespace; what goes in is a run token that is worthless anywhere
            # except the local proxy holding those credentials.
            env["ANTHROPIC_BASE_URL"] = proxy
            env["ANTHROPIC_API_KEY"] = token
            env[RUN_TOKEN_ENV] = token
        else:
            # Started by hand. There is no untrusted party here, so the CLIs get
            # their own config trees; the rest of the home directory is still
            # absent, and the work is still confined to the candidate dir.
            writable += [Path.home() / p for p in STANDALONE_HOME_MOUNTS]

        argv = sandbox.build_argv(work_dir=cwd, home_dir=scratch,
                                  ro_paths=mounts, rw_paths=writable, env=env)
        yield _Sandbox(argv, exe, scratch, home)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_claude_family(role: str, binary: str, claude_model: str | None, effort: str,
                      agent_prompt: Path, task_prompt: str, cwd: Path,
                      ro_paths: list[Path] | None = None) -> dict:
    session_id = str(uuid.uuid4())
    cmd = [
        binary, "--dangerously-skip-permissions",
        "--plugin-dir", str(PLUGIN_ROOT),
        "--output-format", "json",
        "--effort", effort,
        "--append-system-prompt-file", str(agent_prompt),
        "--session-id", session_id,
        "-p", task_prompt,
    ]
    if claude_model:
        cmd[1:1] = ["--model", claude_model]
    log(role, f"start {binary}"
              + (f" [{claude_model}]" if claude_model else "")
              + f" (effort={effort}, session={session_id[:8]})")
    with _sandboxed(binary, cwd, ro_paths or []) as sbx:
        with TranscriptTail(role, session_id, sbx.claude_config_dir):
            proc = subprocess.run(sbx.wrap(cmd), cwd=cwd,
                                  capture_output=True, text=True)
    info = {"session_id": session_id, "exit_code": proc.returncode}
    try:
        out = json.loads(proc.stdout)
        info["cost_usd"] = out.get("total_cost_usd")
        info["is_error"] = out.get("is_error")
        info["num_turns"] = out.get("num_turns")
    except (json.JSONDecodeError, AttributeError):
        info["raw_tail"] = (proc.stdout or "")[-500:] + (proc.stderr or "")[-500:]
    cost = info.get("cost_usd")
    log(role, f"done (exit={proc.returncode}"
              + (f", ${cost:.2f}" if isinstance(cost, (int, float)) else "") + ")")
    return info


def _codex_proxy_args() -> list[str]:
    """Point codex at the credential proxy, when the WebUI gave us one."""
    proxy = os.environ.get(PROXY_URL_ENV)
    if not os.environ.get(RUN_TOKEN_ENV) or not proxy:
        return []
    provider = (f'{{name="agonsr", base_url="{proxy}/v1", '
                f'wire_api="responses", env_key="{RUN_TOKEN_ENV}"}}')
    return ["-c", 'model_provider="agonsr"',
            "-c", f"model_providers.agonsr={provider}"]


def run_codex(role: str, effort: str, agent_prompt: Path,
              task_prompt: str, cwd: Path,
              ro_paths: list[Path] | None = None) -> dict:
    # Written inside the sandbox's private tmpfs and never read back; it exists
    # only because codex insists on somewhere to put its last message.
    out_file = f"/tmp/sr-{role}-{uuid.uuid4().hex[:8]}.txt"
    cmd = [
        "codex", "exec", "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "-m", CODEX_MODEL, "-c", f"model_reasoning_effort={effort}",
        *_codex_proxy_args(),
        "--output-last-message", out_file,
        task_prompt,
    ]
    log(role, f"start codex (effort={effort})")
    info = {"exit_code": None}
    with _sandboxed("codex", cwd, ro_paths or []) as sbx:
        with open(agent_prompt, "r", encoding="utf-8") as agent_fh:
            proc = subprocess.Popen(
                # stderr stays separate: --json puts the events on stdout, and
                # merging the banner back in would undo the point of asking.
                sbx.wrap(cmd), cwd=cwd, stdin=agent_fh,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            session_id = _render_codex_event(role, line)
            if session_id:
                info["session_id"] = session_id
        proc.wait()
    info["exit_code"] = proc.returncode
    log(role, f"done (exit={proc.returncode})")
    return info


def run_subagent(role: str, model: str, effort: str, agent_name: str,
                 task_prompt: str, cwd: Path,
                 ro_paths: list[Path] | None = None) -> dict:
    """`model` is a CLI name ("codex", "claude", "claude-ds", …), optionally
    with a specific model id for claude-family CLIs: "claude:claude-opus-4-6".

    `ro_paths` is everything outside `cwd` this subagent is allowed to read.
    """
    agent_prompt = PLUGIN_ROOT / "agents" / f"{agent_name}.md"
    if model == "codex":
        return run_codex(role, effort, agent_prompt, task_prompt, cwd, ro_paths)
    binary, _, claude_model = model.partition(":")
    return run_claude_family(role, binary, claude_model or None, effort,
                             agent_prompt, task_prompt, cwd, ro_paths)


# ---------------------------------------------------------------- main loop

def extract_score(ansatz_path: Path) -> float | None:
    if not ansatz_path.exists():
        return None
    text = ansatz_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'<review\s+score="([-+0-9.eE]+)"', text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def reached_stop(score: float, stop_at: float | None, direction: str) -> bool:
    if stop_at is None:
        return False
    return score >= stop_at if direction == "maximize" else score <= stop_at


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workdir", default=".", help="project workspace (default: cwd)")
    ap.add_argument("--rounds", type=int, required=True, help="rounds to run this session")
    ap.add_argument("--problem", default="problem.md")
    ap.add_argument("--score-direction", required=True, choices=("maximize", "minimize"))
    ap.add_argument("--proposer-model", required=True)
    ap.add_argument("--proposer-effort", default="max")
    ap.add_argument("--reviewer-model", required=True)
    ap.add_argument("--reviewer-effort", default="max")
    ap.add_argument("--stop-at-score", type=float, default=None,
                    help="stop early once a score reaches this (>= for maximize, <= for minimize)")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    problem = workdir / args.problem
    if not problem.exists():
        fail_hard(workdir, "no_problem", f"{problem} not found")

    search = workdir / SEARCH_DIRNAME
    if (search / "state.json").exists():
        state = json.loads((search / "state.json").read_text(encoding="utf-8"))
        existing = state.get("config", {}).get("score_direction")
        if existing and existing != args.score_direction:
            fail_hard(workdir, "direction_mismatch",
                      f"search was initialized with score direction '{existing}'; "
                      f"start a new project to search in the other direction")
        log("dispatch", f"resuming search in {search}")
    else:
        out = mcts(workdir, "init", "--score-direction", args.score_direction,
                   "--run-dir", str(search))
        log("dispatch", out.strip())

    notes = parse_ignoreme(workdir)
    rounds_done = 0
    last_score = None
    stopped_early = False

    for rnd in range(1, args.rounds + 1):
        cid, cand_dir, ancestors = parse_next(mcts(workdir, "next", "--run-dir", str(search)))
        log("dispatch", f"round {rnd}/{args.rounds}: candidate {cid}")
        clean_dangling(cand_dir)
        cand_dir.mkdir(parents=True, exist_ok=True)

        readable = [problem, workdir / "data"]

        proposer_task = PROPOSER_TEMPLATE.format(
            plugin=PLUGIN_ROOT, problem=problem, ancestors=ancestors,
            workdir=cand_dir, notes=notes["proposer"],
        )
        p_info = run_subagent("proposer", args.proposer_model, args.proposer_effort,
                              "ansatz-proposer", proposer_task, cand_dir,
                              readable + ancestor_paths(ancestors))
        ansatz = cand_dir / "ansatz.md"
        if not ansatz.exists():
            fail_hard(workdir, "no_ansatz",
                      f"proposer finished but {ansatz} does not exist",
                      candidate=cid, round=rnd, proposer=p_info)

        reviewer_task = REVIEWER_TEMPLATE.format(
            plugin=PLUGIN_ROOT, problem=problem,
            workdir=cand_dir, notes=notes["reviewer"],
        )
        r_info = run_subagent("reviewer", args.reviewer_model, args.reviewer_effort,
                              "ansatz-reviewer", reviewer_task, cand_dir, readable)
        score = extract_score(ansatz)
        if score is None:
            fail_hard(workdir, "no_score",
                      f'reviewer finished but no <review score="X"> block in {ansatz}',
                      candidate=cid, round=rnd, reviewer=r_info)

        out = mcts(workdir, "update", "--run-dir", str(search),
                   "--candidate-id", cid, "--score", str(score))
        log("dispatch", out.strip())
        rounds_done, last_score = rnd, score

        if reached_stop(score, args.stop_at_score, args.score_direction):
            log("dispatch", f"score {score} reached stop threshold {args.stop_at_score} — stopping early")
            stopped_early = True
            break

    log("dispatch", "=== best candidates ===")
    print(mcts(workdir, "show", "--run-dir", str(search)), flush=True)
    write_status(workdir,
                 status="stopped_early" if stopped_early else "completed",
                 rounds_done=rounds_done, last_score=last_score)
    log("dispatch", f"finished: {rounds_done} round(s), last score {last_score}")


if __name__ == "__main__":
    main()
