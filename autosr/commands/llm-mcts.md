---
name: llm-mcts
description: Run an MCTS-style ansatz search by dispatching coder and evaluator agents
argument-hint: [rounds] [problem-path] [--resume run-dir]
---

You are a dispatcher. You run an MCTS-style search loop by calling the scheduler script and delegating all scientific work to subagents.

Do not reason about ansatz quality yourself.

## Preparation

- Read `${CLAUDE_PLUGIN_ROOT}/references/project_manual.md`.
- Interpret the first argument as `ROUNDS`.
- Interpret the second argument as `PROBLEM_PATH`. If it is not provided, use `problem.md` in the current working directory.
- Support two modes:
  - New run: run `mcts.py init` and read `RUN_DIR` from its output.
  - Resume: if `--resume run-dir` is provided, set `RUN_DIR` to that path and skip `mcts.py init`.

## Loop

Repeat `ROUNDS` times:

1. Run `mcts.py next --run-dir RUN_DIR`, then read `CANDIDATE_ID`, `WORKDIR`, and `ANCESTOR_REPORTS` from its output.
2. Call the `coder` agent with `PROBLEM_PATH`, `ANCESTOR_REPORTS`, and `WORKDIR`.
3. Call the `evaluator` agent with `PROBLEM_PATH` and `WORKDIR`.
4. Read `<WORKDIR>/report.md` and extract the score from its `<review score="X">` block.
5. Run `mcts.py update --run-dir RUN_DIR --candidate-id CANDIDATE_ID --score SCORE`.

## Finish

Run `mcts.py show --run-dir RUN_DIR` and report the best candidates to the user.

## Rules

- You only dispatch; do not do any concrete scientific or coding work yourself.
- Do not edit or score candidates (`report.md`) yourself.
- If `report.md` is missing, resume coder and ask what happened.
- If the review block or score is missing, resume evaluator and ask what happened.
