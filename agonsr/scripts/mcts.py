#!/usr/bin/env python3
"""MCTS scheduler for AgonSR.

This script does not call LLMs and does not evaluate candidates.
It only manages tree state and returns the next candidate workdir.

Several pipelines may run at once. The allocator is the pending-aware
PW-MCTS of AgonAlpha (arXiv:2608.11250 §4.4, Algorithm 1): each node carries a
completed visit count and an in-flight count, widening and the exploration
term are computed from their sum, a non-root node with a running child opens
no further children, the root is exempt from that so lineages can start in
parallel, and a busy dead end falls back to the root so no worker starves.

Commands:
  mcts.py init --score-direction maximize|minimize [--run-dir RUN_DIR]
  mcts.py next --run-dir RUN_DIR
  mcts.py update --run-dir RUN_DIR --candidate-id ID --score X
  mcts.py discard-pending --run-dir RUN_DIR
  mcts.py show --run-dir RUN_DIR
  mcts.py tree --run-dir RUN_DIR
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT_ID = "root"
DEFAULT_UCB_C = 10.0
DEFAULT_PW_K = 1.0
DEFAULT_PW_ALPHA = 0.5


def _now_stamp() -> str:
    return datetime.now().strftime("%y%m%d_%H%M")


def _state_path(run_dir: Path) -> Path:
    return run_dir / "state.json"


def _candidate_dir(run_dir: Path, cid: str) -> Path:
    return run_dir / cid


def _ansatz_path(run_dir: Path, cid: str) -> Path:
    return _candidate_dir(run_dir, cid) / "ansatz.md"


def _lock_path(run_dir: Path) -> Path:
    return run_dir / ".state.lock"


@contextmanager
def _state_lock(run_dir: Path):
    """Serialize every read-modify-write of the tree.

    Concurrent pipelines each call `next` and `update`, so without this two of
    them can select against the same snapshot and be handed the same node.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    with _lock_path(run_dir).open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_state(run_dir: Path) -> dict:
    with _state_path(run_dir).open(encoding="utf-8") as f:
        state = json.load(f)
    # Candidate ids come from the node set, not a counter that can drift out of
    # step with it when a write is lost.
    state.pop("next_candidate_num", None)
    return state


def _save_state(run_dir: Path, state: dict) -> None:
    """Write via a temporary file and rename.

    A reader holding the lock still sees a whole file if the writer dies
    halfway, which a plain truncate-and-write does not give.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=run_dir)
    tmp_path = Path(tmp_name)
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _state_path(run_dir))
    finally:
        tmp_path.unlink(missing_ok=True)


def _new_node(node_id: str, parent: str | None, depth: int, status: str = "open") -> dict:
    return {
        "id": node_id,
        "parent": parent,
        "children": [],
        "depth": depth,
        "visits": 0,
        "score": None,
        "status": status,
    }


def _initial_state(run_dir: Path, score_direction: str, ucb_c: float,
                   pw_k: float, pw_alpha: float) -> dict:
    return {
        "method": "llm-mcts",
        "run_dir": str(run_dir),
        "config": {
            "score_direction": score_direction,
            "ucb_c": ucb_c,
            "pw_k": pw_k,
            "pw_alpha": pw_alpha,
        },
        "nodes": {ROOT_ID: _new_node(ROOT_ID, None, 0, status="root")},
    }


def _next_candidate_id(state: dict) -> str:
    used = [int(cid) for cid in state["nodes"] if cid != ROOT_ID]
    return f"{max(used, default=0) + 1:04d}"


def _score_direction(state: dict) -> str:
    direction = state.get("config", {}).get("score_direction")
    if direction not in {"maximize", "minimize"}:
        raise SystemExit("missing or invalid config.score_direction in state.json")
    return direction


def _score_history(state: dict) -> list[float]:
    return [
        float(node["score"])
        for cid, node in state["nodes"].items()
        if cid != ROOT_ID
        and node.get("status") == "done"
        and node.get("score") is not None
    ]


def _percentile_reward(score: float, scores: list[float], direction: str) -> float:
    """Where this score sits in the completed population, as a midrank.

    Counting `s <= score` gave every member of a tie the rank of the whole
    group, so candidates that all scored the same each took full marks. The
    midrank splits a tie across the ranks it spans, and needs no special case
    for a single score: one score against itself is rank 0.5, or 5.0.
    """
    if not scores:
        raise ValueError("scores must not be empty")
    if not math.isfinite(score) or any(not math.isfinite(s) for s in scores):
        raise ValueError("scores must be finite")
    if direction == "maximize":
        below = sum(1 for s in scores if s < score)
        at_or_below = sum(1 for s in scores if s <= score)
    else:
        below = sum(1 for s in scores if s > score)
        at_or_below = sum(1 for s in scores if s >= score)
    rank = (below + at_or_below) / 2
    return 10.0 * rank / len(scores)


def _node_rewards(state: dict) -> dict[str, float]:
    scores = _score_history(state)
    if not scores:
        return {}
    direction = _score_direction(state)
    return {
        cid: _percentile_reward(float(node["score"]), scores, direction)
        for cid, node in state["nodes"].items()
        if cid != ROOT_ID
        and node.get("status") == "done"
        and node.get("score") is not None
    }


def _subtree_reward_sum(state: dict, rewards: dict[str, float], node_id: str,
                        _seen: set[str] | None = None) -> float:
    """Reward accumulated by completed work in this subtree.

    In-flight children contribute nothing: they have no result yet, and
    counting them would let a branch look valuable for work not yet done.
    """
    if _seen is None:
        _seen = set()
    if node_id in _seen:
        raise ValueError(f"cycle in subtree: {node_id}")
    _seen.add(node_id)
    total = rewards.get(node_id, 0.0)
    for child_id in state["nodes"][node_id]["children"]:
        if state["nodes"][child_id].get("status") == "done":
            total += _subtree_reward_sum(state, rewards, child_id, _seen)
    return total


def _pending_counts(state: dict) -> dict[str, int]:
    """How much in-flight work sits beneath each node.

    A running pipeline is credited to every node on its ancestor chain, so
    this is what stops several workers piling into the same lineage.
    """
    nodes = state["nodes"]
    counts = {node_id: 0 for node_id in nodes}
    for node_id, node in nodes.items():
        if node.get("status") != "pending":
            continue
        cur: str | None = node_id
        seen: set[str] = set()
        while cur is not None:
            if cur in seen:
                raise ValueError(f"cycle in parent path: {node_id}")
            if cur not in nodes:
                raise ValueError(f"unknown parent in path from: {node_id}")
            seen.add(cur)
            counts[cur] += 1
            cur = nodes[cur]["parent"]
    return counts


def _done_children(state: dict, node_id: str) -> list[str]:
    nodes = state["nodes"]
    return [child_id for child_id in nodes[node_id]["children"]
            if nodes[child_id].get("status") == "done"]


def _has_pending_child(state: dict, node_id: str) -> bool:
    nodes = state["nodes"]
    return any(nodes[child_id].get("status") == "pending"
               for child_id in nodes[node_id]["children"])


def _ucb(state: dict, rewards: dict[str, float], pending_counts: dict[str, int],
         parent_id: str, child_id: str, c: float) -> float:
    """Pending-aware upper confidence bound.

    The exploitation term divides by completed visits only, so a result is not
    diluted by work still running. The exploration term counts in-flight work
    in both parent and child, which is what keeps the next worker away from a
    branch that already has one.
    """
    child = state["nodes"][child_id]
    if child["visits"] == 0:
        return float("inf")
    q = _subtree_reward_sum(state, rewards, child_id) / child["visits"]
    parent_samples = state["nodes"][parent_id]["visits"] + pending_counts[parent_id]
    child_samples = child["visits"] + pending_counts[child_id]
    return q + c * math.sqrt(math.log(max(parent_samples, 1)) / child_samples)


def _should_widen(state: dict, pending_counts: dict[str, int], node_id: str,
                  pw_k: float, pw_alpha: float) -> bool:
    """Progressive widening over completed *and* in-flight evidence."""
    node = state["nodes"][node_id]
    done_child_count = len(_done_children(state, node_id))
    samples = node["visits"] + pending_counts[node_id]
    return done_child_count < pw_k * (max(samples, 1) ** pw_alpha)


def _select_parent(state: dict) -> str:
    rewards = _node_rewards(state)
    pending_counts = _pending_counts(state)
    c = state["config"]["ucb_c"]
    pw_k = state["config"]["pw_k"]
    pw_alpha = state["config"]["pw_alpha"]

    def search(node_id: str) -> str | None:
        # A non-root node may have at most one direct pending child. While that
        # child is running, the node itself cannot be expanded again, although
        # its completed descendants remain selectable.
        can_expand = node_id == ROOT_ID or not _has_pending_child(state, node_id)
        if can_expand and _should_widen(state, pending_counts, node_id, pw_k, pw_alpha):
            return node_id

        done_children = _done_children(state, node_id)
        ranked_children = sorted(
            done_children,
            key=lambda child_id: _ucb(state, rewards, pending_counts, node_id, child_id, c),
            reverse=True,
        )
        for child_id in ranked_children:
            selected = search(child_id)
            if selected is not None:
                return selected
        return None

    # If every non-root branch is busy, root is the deliberate fallback even
    # when its progressive-widening condition is currently closed.
    return search(ROOT_ID) or ROOT_ID


def _backprop_visit(state: dict, node_id: str) -> None:
    nodes = state["nodes"]
    seen: set[str] = set()
    cur: str | None = node_id
    while cur is not None:
        if cur in seen:
            raise ValueError(f"cycle in parent path: {node_id}")
        seen.add(cur)
        nodes[cur]["visits"] += 1
        cur = nodes[cur]["parent"]


def _ancestor_ids(state: dict, node_id: str) -> list[str]:
    nodes = state["nodes"]
    out: list[str] = []
    seen: set[str] = {node_id}
    cur = nodes[node_id]["parent"]
    while cur is not None and cur != ROOT_ID:
        if cur in seen:
            raise ValueError(f"cycle in parent path: {node_id}")
        seen.add(cur)
        out.append(cur)
        cur = nodes[cur]["parent"]
    return out


def _print_next(state: dict, run_dir: Path, cid: str) -> None:
    print(f"CANDIDATE_ID: {cid}")
    print(f"WORKDIR: {_candidate_dir(run_dir, cid)}")
    ancestors = _ancestor_ids(state, cid)
    print("ANCESTOR_REPORTS:")
    if not ancestors:
        print("none")
        return
    QUALIFIERS = ("father", "grandfather")
    for i, aid in enumerate(ancestors):
        suffix = f" ({QUALIFIERS[i]})" if i < len(QUALIFIERS) else ""
        print(f"- ancestor {i + 1}{suffix}: {_ansatz_path(run_dir, aid)}")


def cmd_init(args: argparse.Namespace) -> None:
    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        runs_dir = Path("runs")
        run_dir = runs_dir / f"llm-mcts_{_now_stamp()}"
        suffix = 1
        while run_dir.exists():
            run_dir = runs_dir / f"llm-mcts_{_now_stamp()}_{suffix}"
            suffix += 1
    with _state_lock(run_dir):
        if _state_path(run_dir).exists():
            raise SystemExit(f"refusing to init: {run_dir} already has a state file")
        _save_state(run_dir, _initial_state(run_dir, args.score_direction,
                                            args.ucb_c, args.pw_k, args.pw_alpha))
    print(f"RUN_DIR: {run_dir}")


def cmd_discard_pending(args: argparse.Namespace) -> None:
    """Give up on candidates that were in flight when a run died.

    Not the same as resuming them: a half-finished workdir has no way to tell
    an interrupted attempt from one still being worked on, and with several
    pipelines running there is always something in flight.
    """
    run_dir = Path(args.run_dir)
    with _state_lock(run_dir):
        if not _state_path(run_dir).exists():
            return
        state = _load_state(run_dir)
        nodes = state["nodes"]
        pending_ids = sorted(cid for cid, node in nodes.items()
                             if cid != ROOT_ID and node.get("status") == "pending")
        for cid in pending_ids:
            if nodes[cid]["children"]:
                raise SystemExit(f"pending candidate has children: {cid}")
            nodes[cid]["status"] = "discarded"
        if pending_ids:
            _save_state(run_dir, state)

        discarded = sorted(_candidate_dir(run_dir, cid)
                           for cid, node in nodes.items()
                           if cid != ROOT_ID and node.get("status") == "discarded"
                           and _candidate_dir(run_dir, cid).exists())
        for workdir in discarded:
            print(f"DISCARDED_WORKDIR: {workdir}")


def cmd_next(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    with _state_lock(run_dir):
        # AgonAlpha's version initialises a missing tree here. This one cannot:
        # the score direction has no safe default, and guessing it would run a
        # whole search in the wrong direction without saying so.
        if not _state_path(run_dir).exists():
            raise SystemExit(f"no state file in {run_dir}; run `mcts.py init` first")
        state = _load_state(run_dir)
        parent_id = _select_parent(state)
        parent = state["nodes"][parent_id]
        cid = _next_candidate_id(state)
        state["nodes"][cid] = _new_node(cid, parent_id, parent["depth"] + 1,
                                        status="pending")
        parent["children"].append(cid)
        _candidate_dir(run_dir, cid).mkdir(parents=True, exist_ok=True)
        _save_state(run_dir, state)
        _print_next(state, run_dir, cid)


def cmd_update(args: argparse.Namespace) -> None:
    score = args.score
    if not math.isfinite(score):
        raise SystemExit("score must be finite")

    run_dir = Path(args.run_dir)
    with _state_lock(run_dir):
        state = _load_state(run_dir)
        cid = args.candidate_id
        if cid not in state["nodes"] or cid == ROOT_ID:
            raise SystemExit(f"unknown candidate id: {cid}")
        node = state["nodes"][cid]
        if node.get("status") == "done":
            # Re-reporting the same result is a retry, not a second visit.
            if float(node["score"]) == score:
                print(f"ALREADY_UPDATED: {cid} score={score:.4g}")
                return
            raise SystemExit(f"candidate already has a different score: {cid}")
        if node.get("status") != "pending":
            raise SystemExit(f"candidate is not pending: {cid}")

        node["score"] = score
        node["status"] = "done"
        _backprop_visit(state, cid)
        _save_state(run_dir, state)
        print(f"UPDATED: {cid} score={score:.4g}")


def cmd_show(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    with _state_lock(run_dir):
        state = _load_state(run_dir)
    direction = _score_direction(state)
    rows = []
    for cid, node in state["nodes"].items():
        if cid == ROOT_ID or node.get("score") is None:
            continue
        rows.append((float(node["score"]), cid, node["depth"],
                     str(_ansatz_path(run_dir, cid))))
    rows.sort(key=lambda row: row[0], reverse=direction == "maximize")
    print(f"RUN_DIR: {run_dir}")
    print(f"SCORE_DIRECTION: {direction}")
    print("TOP_CANDIDATES:")
    for score, cid, depth, ansatz in rows[:10]:
        print(f"- {cid}: score={score:.4g} depth={depth} ansatz={ansatz}")


def _format_score(score: object) -> str:
    if score is None:
        return "pending"
    return f"{float(score):.4g}"


def _looks_like_formula(text: str) -> bool:
    tokens = ("=", "_", "^", "\\frac", "\\sqrt", "\\exp", "\\log(", "\\max(", "\\min(", "|")
    return any(token in text for token in tokens)


def _candidate_formula(run_dir: Path, cid: str) -> str:
    path = _ansatz_path(run_dir, cid)
    if not path.exists():
        return "missing ansatz"
    text = path.read_text(errors="ignore")
    match = re.search(r"^## One sentence\s*(.*?)(?=^## |\Z)", text, re.M | re.S)
    section = match.group(1) if match else text
    formulas = []
    formulas.extend(re.findall(r"\$\$(.*?)\$\$", section, re.S))
    formulas.extend(re.findall(r"\\\((.*?)\\\)", section, re.S))
    formulas.extend(re.findall(r"(?<!\$)\$(?!\$)(.*?)(?<!\$)\$(?!\$)", section, re.S))
    formulas = [re.sub(r"\s+", " ", f).strip() for f in formulas]
    formulas = [f for f in formulas if f and _looks_like_formula(f)]
    return "; ".join(formulas) if formulas else "no formula"


def cmd_tree(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    if not _state_path(run_dir).exists():
        raise SystemExit("no state file found")
    with _state_lock(run_dir):
        state = _load_state(run_dir)
    nodes = state["nodes"]

    def label(node_id: str) -> str:
        node = nodes[node_id]
        status = node.get("status", "?")
        tag = f"{status}, v={node.get('visits', 0)}"
        score = node.get("score")
        if score is not None:
            tag += f", s={_format_score(score)}"
        parts = [node_id, f"[{tag}]"]
        if node_id != ROOT_ID and status == "done":
            parts.append(f"| {_candidate_formula(run_dir, node_id)}")
        return " ".join(parts)

    def walk(node_id: str, prefix: str) -> None:
        children = nodes[node_id].get("children", [])
        for i, child_id in enumerate(children):
            is_last = i == len(children) - 1
            print(f"{prefix}{'└── ' if is_last else '├── '}{label(child_id)}")
            walk(child_id, prefix + ("    " if is_last else "│   "))

    print(label(ROOT_ID))
    walk(ROOT_ID, "")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--run-dir")
    p_init.add_argument("--score-direction", required=True,
                        choices=["maximize", "minimize"])
    p_init.add_argument("--ucb-c", type=float, default=DEFAULT_UCB_C)
    p_init.add_argument("--pw-k", type=float, default=DEFAULT_PW_K)
    p_init.add_argument("--pw-alpha", type=float, default=DEFAULT_PW_ALPHA)
    p_init.set_defaults(func=cmd_init)

    p_discard = sub.add_parser("discard-pending")
    p_discard.add_argument("--run-dir", required=True)
    p_discard.set_defaults(func=cmd_discard_pending)

    p_next = sub.add_parser("next")
    p_next.add_argument("--run-dir", required=True)
    p_next.set_defaults(func=cmd_next)

    p_update = sub.add_parser("update")
    p_update.add_argument("--run-dir", required=True)
    p_update.add_argument("--candidate-id", required=True)
    p_update.add_argument("--score", required=True, type=float)
    p_update.set_defaults(func=cmd_update)

    p_show = sub.add_parser("show")
    p_show.add_argument("--run-dir", required=True)
    p_show.set_defaults(func=cmd_show)

    p_tree = sub.add_parser("tree")
    p_tree.add_argument("--run-dir", required=True)
    p_tree.set_defaults(func=cmd_tree)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
