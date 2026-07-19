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
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SEARCH_DIRNAME = "search"
CODEX_MODEL = "gpt-5.6-sol"

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

def _transcript_roots() -> list[Path]:
    roots = []
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        roots.append(Path(cfg) / "projects")
    roots.append(Path.home() / ".claude" / "projects")
    return roots


def _find_transcript(session_id: str) -> Path | None:
    for root in _transcript_roots():
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


class TranscriptTail:
    """Streams a claude session transcript into the log as it grows."""

    def __init__(self, role: str, session_id: str):
        self.role = role
        self.session_id = session_id
        self.path: Path | None = None
        self.offset = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def poll(self) -> None:
        if self.path is None:
            self.path = _find_transcript(self.session_id)
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


def run_claude_family(role: str, binary: str, claude_model: str | None, effort: str,
                      agent_prompt: Path, task_prompt: str, cwd: Path) -> dict:
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
    with TranscriptTail(role, session_id):
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
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


def run_codex(role: str, effort: str, agent_prompt: Path,
              task_prompt: str, cwd: Path) -> dict:
    out_dir = Path("/tmp") / os.environ.get("USER", "agonsr")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"sr-{role}-{uuid.uuid4().hex[:8]}.txt"
    cmd = [
        "codex", "exec", "--dangerously-bypass-approvals-and-sandbox",
        "-m", CODEX_MODEL, "-c", f"model_reasoning_effort={effort}",
        "--output-last-message", str(out_file),
        task_prompt,
    ]
    log(role, f"start codex (effort={effort})")
    info = {"exit_code": None}
    with open(agent_prompt, "r", encoding="utf-8") as agent_fh:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=agent_fh,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        m = re.search(r"session id:?\s*([0-9a-f-]{8,})", line, re.IGNORECASE)
        if m:
            info["session_id"] = m.group(1)
        log(role, line[:200])
    proc.wait()
    info["exit_code"] = proc.returncode
    if out_file.exists():
        out_file.unlink()
    log(role, f"done (exit={proc.returncode})")
    return info


def run_subagent(role: str, model: str, effort: str, agent_name: str,
                 task_prompt: str, cwd: Path) -> dict:
    """`model` is a CLI name ("codex", "claude", "claude-ds", …), optionally
    with a specific model id for claude-family CLIs: "claude:claude-opus-4-6"."""
    agent_prompt = PLUGIN_ROOT / "agents" / f"{agent_name}.md"
    if model == "codex":
        return run_codex(role, effort, agent_prompt, task_prompt, cwd)
    binary, _, claude_model = model.partition(":")
    return run_claude_family(role, binary, claude_model or None, effort,
                             agent_prompt, task_prompt, cwd)


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

        proposer_task = PROPOSER_TEMPLATE.format(
            plugin=PLUGIN_ROOT, problem=problem, ancestors=ancestors,
            workdir=cand_dir, notes=notes["proposer"],
        )
        p_info = run_subagent("proposer", args.proposer_model, args.proposer_effort,
                              "ansatz-proposer", proposer_task, cand_dir)
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
                              "ansatz-reviewer", reviewer_task, cand_dir)
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
