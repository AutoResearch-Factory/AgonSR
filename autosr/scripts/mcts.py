#!/usr/bin/env python3
"""Agentic MCTS scheduler for AutoSR.

This script does not call LLMs and does not evaluate candidates.
It only manages tree state and returns the next candidate workdir.

Commands:
  mcts.py init
  mcts.py next --run-dir RUN_DIR
  mcts.py update --run-dir RUN_DIR --candidate-id ID --score X
  mcts.py show --run-dir RUN_DIR
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

ROOT_ID = "root"
DEFAULT_UCB_C = 1.41421356237
DEFAULT_PW_K = 1.0
DEFAULT_PW_ALPHA = 0.5


def _now_stamp() -> str:
    return datetime.now().strftime("%y%m%d_%H%M")


def _state_path(run_dir: Path) -> Path:
    return run_dir / "state.json"


def _candidates_dir(run_dir: Path) -> Path:
    return run_dir / "candidates"


def _candidate_dir(run_dir: Path, cid: str) -> Path:
    return _candidates_dir(run_dir) / cid


def _report_path(run_dir: Path, cid: str) -> Path:
    return _candidate_dir(run_dir, cid) / "report.md"


def _load_state(run_dir: Path) -> dict:
    with _state_path(run_dir).open() as f:
        return json.load(f)


def _save_state(run_dir: Path, state: dict) -> None:
    _state_path(run_dir).write_text(json.dumps(state, indent=2) + "\n")


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


def _next_candidate_id(state: dict) -> str:
    state["next_candidate_num"] += 1
    return f"{state['next_candidate_num']:04d}"


def _score_history(state: dict) -> list[float]:
    scores = []
    for cid, node in state["nodes"].items():
        if cid != ROOT_ID and node.get("score") is not None:
            scores.append(float(node["score"]))
    return scores


def _percentile_reward(score: float, scores: list[float]) -> float:
    if len(scores) <= 1:
        return 5.0
    rank = sum(1 for s in scores if s <= score)
    return 10.0 * rank / len(scores)


def _node_rewards(state: dict) -> dict[str, float]:
    scores = _score_history(state)
    rewards: dict[str, float] = {}
    for cid, node in state["nodes"].items():
        if cid == ROOT_ID or node.get("score") is None:
            continue
        rewards[cid] = _percentile_reward(float(node["score"]), scores)
    return rewards


def _subtree_reward_sum(state: dict, rewards: dict[str, float], node_id: str) -> float:
    total = rewards.get(node_id, 0.0)
    for child_id in state["nodes"][node_id]["children"]:
        total += _subtree_reward_sum(state, rewards, child_id)
    return total


def _ucb(state: dict, rewards: dict[str, float], child_id: str, parent_visits: int, c: float) -> float:
    child = state["nodes"][child_id]
    if child["visits"] == 0:
        return float("inf")
    q = _subtree_reward_sum(state, rewards, child_id) / child["visits"]
    return q + c * math.sqrt(math.log(max(parent_visits, 1)) / child["visits"])


def _should_widen(node: dict, pw_k: float, pw_alpha: float) -> bool:
    return len(node["children"]) < pw_k * (max(node["visits"], 1) ** pw_alpha)


def _select_parent(state: dict) -> str:
    nodes = state["nodes"]
    rewards = _node_rewards(state)
    c = state["config"]["ucb_c"]
    pw_k = state["config"]["pw_k"]
    pw_alpha = state["config"]["pw_alpha"]

    cur_id = ROOT_ID
    while True:
        cur = nodes[cur_id]
        if _should_widen(cur, pw_k, pw_alpha):
            return cur_id
        if not cur["children"]:
            return cur_id
        parent_visits = max(cur["visits"], 1)
        cur_id = max(cur["children"], key=lambda cid: _ucb(state, rewards, cid, parent_visits, c))


def _backprop_visit(state: dict, node_id: str) -> None:
    nodes = state["nodes"]
    cur: str | None = node_id
    while cur is not None:
        nodes[cur]["visits"] += 1
        cur = nodes[cur]["parent"]


def _ancestor_ids(state: dict, node_id: str) -> list[str]:
    nodes = state["nodes"]
    out: list[str] = []
    cur = nodes[node_id]["parent"]
    while cur is not None and cur != ROOT_ID:
        out.append(cur)
        cur = nodes[cur]["parent"]
    return out


def _print_next(run_dir: Path, cid: str) -> None:
    print(f"CANDIDATE_ID: {cid}")
    print(f"WORKDIR: {_candidate_dir(run_dir, cid)}")
    ancestors = _ancestor_ids(_load_state(run_dir), cid)
    print("ANCESTOR_REPORTS:")
    if not ancestors:
        print("none")
        return
    labels = ["father", "grandfather"]
    for i, aid in enumerate(ancestors):
        label = labels[i] if i < len(labels) else f"ancestor {i + 1}"
        print(f"- {label}: {_report_path(run_dir, aid)}")


def cmd_init(args: argparse.Namespace) -> None:
    runs_dir = Path("runs")
    run_dir = runs_dir / f"llm-mcts_{_now_stamp()}"
    suffix = 1
    while run_dir.exists():
        run_dir = runs_dir / f"llm-mcts_{_now_stamp()}_{suffix}"
        suffix += 1

    _candidates_dir(run_dir).mkdir(parents=True)
    state = {
        "method": "llm-mcts",
        "run_dir": str(run_dir),
        "next_candidate_num": 0,
        "config": {
            "ucb_c": args.ucb_c,
            "pw_k": args.pw_k,
            "pw_alpha": args.pw_alpha,
        },
        "nodes": {ROOT_ID: _new_node(ROOT_ID, None, 0, status="root")},
    }
    _save_state(run_dir, state)
    print(f"RUN_DIR: {run_dir}")


def cmd_next(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    state = _load_state(run_dir)

    for cid, node in sorted(state["nodes"].items()):
        if cid != ROOT_ID and node.get("status") == "pending":
            _print_next(run_dir, cid)
            return

    parent_id = _select_parent(state)
    parent = state["nodes"][parent_id]
    cid = _next_candidate_id(state)
    state["nodes"][cid] = _new_node(cid, parent_id, parent["depth"] + 1, status="pending")
    parent["children"].append(cid)
    _candidate_dir(run_dir, cid).mkdir(parents=True)
    _save_state(run_dir, state)
    _print_next(run_dir, cid)


def cmd_update(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    state = _load_state(run_dir)
    cid = args.candidate_id
    if cid not in state["nodes"] or cid == ROOT_ID:
        raise SystemExit(f"unknown candidate id: {cid}")
    score = float(args.score)
    if not (0.0 <= score <= 10.0):
        raise SystemExit("score must be between 0 and 10")

    node = state["nodes"][cid]
    node["score"] = score
    node["status"] = "done"
    _backprop_visit(state, cid)
    _save_state(run_dir, state)
    print(f"UPDATED: {cid} score={score:.4g}")


def cmd_show(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    state = _load_state(run_dir)
    rows = []
    for cid, node in state["nodes"].items():
        if cid == ROOT_ID or node.get("score") is None:
            continue
        rows.append((float(node["score"]), cid, node["depth"], str(_report_path(run_dir, cid))))
    rows.sort(reverse=True)
    print(f"RUN_DIR: {run_dir}")
    print("TOP_CANDIDATES:")
    for score, cid, depth, report in rows[:10]:
        print(f"- {cid}: score={score:.4g} depth={depth} report={report}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--ucb-c", type=float, default=DEFAULT_UCB_C)
    p_init.add_argument("--pw-k", type=float, default=DEFAULT_PW_K)
    p_init.add_argument("--pw-alpha", type=float, default=DEFAULT_PW_ALPHA)
    p_init.set_defaults(func=cmd_init)

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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
