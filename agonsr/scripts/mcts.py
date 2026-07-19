#!/usr/bin/env python3
"""Agentic MCTS scheduler for AgonSR.

This script does not call LLMs and does not evaluate candidates.
It only manages tree state and returns the next candidate workdir.

Commands:
  mcts.py init --score-direction maximize|minimize
  mcts.py next --run-dir RUN_DIR
  mcts.py update --run-dir RUN_DIR --candidate-id ID --score X
  mcts.py show --run-dir RUN_DIR
  mcts.py tree --run-dir RUN_DIR
"""

from __future__ import annotations

import argparse
import json
import math
import re
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


def _score_direction(state: dict) -> str:
    direction = state.get("config", {}).get("score_direction")
    if direction not in {"maximize", "minimize"}:
        raise SystemExit("missing or invalid config.score_direction in state.json")
    return direction


def _percentile_reward(score: float, scores: list[float], direction: str) -> float:
    if len(scores) <= 1:
        return 5.0
    if direction == "maximize":
        rank = sum(1 for s in scores if s <= score)
    else:
        rank = sum(1 for s in scores if s >= score)
    return 10.0 * rank / len(scores)


def _node_rewards(state: dict) -> dict[str, float]:
    direction = _score_direction(state)
    scores = _score_history(state)
    rewards: dict[str, float] = {}
    for cid, node in state["nodes"].items():
        if cid == ROOT_ID or node.get("score") is None:
            continue
        rewards[cid] = _percentile_reward(float(node["score"]), scores, direction)
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
    labels = {0: "ancestor 1 (father)", 1: "ancestor 2 (grandfather)"}
    for i, aid in enumerate(ancestors):
        label = labels.get(i, f"ancestor {i + 1}")
        print(f"- {label}: {_ansatz_path(run_dir, aid)}")


def _format_score(score: object) -> str:
    if score is None:
        return "pending"
    return f"{float(score):.4g}"


def _looks_like_formula(text: str) -> bool:
    tokens = ("=", "_", "^", "\\frac", "\\sqrt", "\\exp", "\\log(", "\\max(", "\\min(", "|")
    return any(token in text for token in tokens)


def _candidate_formula(run_dir: Path, cid: str) -> str:
    for name in ("ansatz.md", "report.md"):
        path = _candidate_dir(run_dir, cid) / name
        if not path.exists():
            continue
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
    return "missing ansatz"


def _tree_lines(state: dict, run_dir: Path, node_id: str, prefix: str = "", is_last: bool = True) -> list[str]:
    node = state["nodes"][node_id]
    if node_id == ROOT_ID:
        lines = ["root"]
    else:
        label = f"{node_id} score={_format_score(node.get('score'))} | {_candidate_formula(run_dir, node_id)}"
        connector = "└── " if is_last else "├── "
        lines = [prefix + connector + label]

    children = node.get("children", [])
    child_prefix = prefix if node_id == ROOT_ID else prefix + ("    " if is_last else "│   ")
    for i, child_id in enumerate(children):
        lines.extend(_tree_lines(state, run_dir, child_id, child_prefix, i == len(children) - 1))
    return lines


def _sanitize_mathtext(formula: str) -> str:
    formula = re.sub(r"\s+", " ", formula).strip()
    formula = formula.replace("\\tfrac", "\\frac").replace("\\dfrac", "\\frac")
    formula = re.sub(r"\\(?:left|right|bigl|bigr|Bigl|Bigr|big|Big)\s*", "", formula)
    formula = re.sub(r"\\[,;:!]", "", formula)
    formula = re.sub(r"\\(?:quad|qquad)\s*", " ", formula)
    formula = re.sub(r"\\text\s*\{([^{}]*)\}", r"\\mathrm{\1}", formula)
    formula = re.sub(r"\\operatorname\s*\{([^{}]*)\}", r"\\mathrm{\1}", formula)
    formula = re.sub(r"(?<!\\)%", r"\\%", formula)
    return formula


def _render_formula(formula: str, parser: object) -> str:
    rendered = _sanitize_mathtext(formula)
    try:
        parser.parse(f"${rendered}$", dpi=120)
    except Exception:
        return formula
    return f"${rendered}$"


def _image_line(line: str, parser: object) -> str:
    if " | " not in line:
        return line
    head, formula_text = line.split(" | ", 1)
    if formula_text in {"missing ansatz", "no formula"}:
        return line
    formulas = [f.strip() for f in re.split(r"(?<!\\);", formula_text) if f.strip()]
    if not formulas:
        return line
    return f"{head} | " + "; ".join(_render_formula(formula, parser) for formula in formulas)


def _save_tree_image(run_dir: Path, lines: list[str]) -> Path | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.mathtext import MathTextParser
    except Exception as exc:
        print(f"TREE_IMAGE: skipped ({exc})")
        return None

    parser = MathTextParser("path")
    image_lines = [_image_line(line, parser) for line in lines]
    row_height = 0.34
    fig_height = max(2.0, row_height * len(image_lines) + 0.4)
    fig_width = 12.0
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, len(image_lines))
    for i, line in enumerate(image_lines):
        y = len(image_lines) - i - 0.75
        ax.text(
            0.01,
            y,
            line,
            fontsize=9,
            fontfamily="DejaVu Sans Mono",
            va="center",
            ha="left",
            clip_on=False,
        )
    image_path = run_dir / "tree.png"
    try:
        fig.savefig(image_path, dpi=200, bbox_inches="tight", pad_inches=0.2)
    except Exception:
        plt.close(fig)
        fig, ax = plt.subplots(figsize=(fig_width, fig_height))
        ax.axis("off")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, len(lines))
        for i, line in enumerate(lines):
            y = len(lines) - i - 0.75
            ax.text(
                0.01,
                y,
                line,
                fontsize=9,
                fontfamily="DejaVu Sans Mono",
                va="center",
                ha="left",
                clip_on=False,
            )
        fig.savefig(image_path, dpi=200, bbox_inches="tight", pad_inches=0.2)
    finally:
        plt.close(fig)
    return image_path


def cmd_init(args: argparse.Namespace) -> None:
    if args.run_dir:
        run_dir = Path(args.run_dir)
        if _state_path(run_dir).exists():
            raise SystemExit(f"refusing to init: {run_dir} already has a state file")
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        runs_dir = Path("runs")
        run_dir = runs_dir / f"llm-mcts_{_now_stamp()}"
        suffix = 1
        while run_dir.exists():
            run_dir = runs_dir / f"llm-mcts_{_now_stamp()}_{suffix}"
            suffix += 1
        run_dir.mkdir(parents=True)
    state = {
        "method": "llm-mcts",
        "run_dir": str(run_dir),
        "next_candidate_num": 0,
        "config": {
            "score_direction": args.score_direction,
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
            _candidate_dir(run_dir, cid).mkdir(parents=True, exist_ok=True)
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

    node = state["nodes"][cid]
    node["score"] = score
    node["status"] = "done"
    _backprop_visit(state, cid)
    _save_state(run_dir, state)
    print(f"UPDATED: {cid} score={score:.4g}")


def cmd_show(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    state = _load_state(run_dir)
    direction = _score_direction(state)
    rows = []
    for cid, node in state["nodes"].items():
        if cid == ROOT_ID or node.get("score") is None:
            continue
        rows.append((float(node["score"]), cid, node["depth"], str(_ansatz_path(run_dir, cid))))
    rows.sort(key=lambda row: row[0], reverse=direction == "maximize")
    print(f"RUN_DIR: {run_dir}")
    print(f"SCORE_DIRECTION: {direction}")
    print("TOP_CANDIDATES:")
    for score, cid, depth, ansatz in rows[:10]:
        print(f"- {cid}: score={score:.4g} depth={depth} ansatz={ansatz}")


def cmd_tree(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    state = _load_state(run_dir)
    print(f"RUN_DIR: {run_dir}")
    print(f"SCORE_DIRECTION: {_score_direction(state)}")
    lines = _tree_lines(state, run_dir, ROOT_ID)
    for line in lines:
        print(line)
    image_path = _save_tree_image(run_dir, lines)
    if image_path is not None:
        print(f"TREE_IMAGE: {image_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--score-direction", required=True, choices=("maximize", "minimize"))
    p_init.add_argument("--run-dir", default=None,
                        help="create the run at this fixed path instead of runs/llm-mcts_<timestamp>")
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

    p_tree = sub.add_parser("tree")
    p_tree.add_argument("--run-dir", required=True)
    p_tree.set_defaults(func=cmd_tree)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
