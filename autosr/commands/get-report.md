---
name: get-report
description: Generate a final AgonSR report from a completed llm-mcts run
argument-hint: "[run-dir] [free-form user instructions]"
---

Generate a polished final report for an existing AgonSR symbolic-regression run.

## Argument parsing

- Treat the first argument as `RUN_DIR` only if it resolves to an existing directory that contains `state.json`, or to an existing directory under `runs/` that contains `state.json`.
- Treat all remaining text, and all text when no explicit `RUN_DIR` is found, as `USER_NOTES`. `USER_NOTES` is free-form: preserve its intent regardless of format or length.
- If `RUN_DIR` is not provided:
  1. If the current directory or one of its ancestors is an `llm-mcts_*` run directory containing `state.json`, use that directory.
  2. Otherwise, from the current problem workspace, choose the latest `runs/llm-mcts_*` directory. Use directory name timestamp order first; use modification time only to break ties.
- Do not read `state.json` or candidate `ansatz.md` files until after the MCTS tree image step below.

## Required workflow

1. Read `${CLAUDE_PLUGIN_ROOT}/references/project_manual.md`.
2. Resolve `RUN_DIR` as above.
3. Run `${CLAUDE_PLUGIN_ROOT}/scripts/mcts.py tree --run-dir RUN_DIR` before reading `state.json`. This must generate or refresh `RUN_DIR/tree.png`; treat this file as a raw draft only, not as the final report figure.
4. Read `RUN_DIR/state.json`.
5. Read every completed candidate's `ansatz.md` in this run. Read enough candidate artifacts only when an `ansatz.md` explicitly refers to them and they are needed to verify what the final report says.
6. Read the problem statement if it can be located unambiguously:
   - Prefer `problem.md` in the problem workspace that contains `runs/`.
   - If unavailable, infer only from candidate reports and state clearly that the original problem statement was not found.
7. Determine score direction from `state.json`, rank completed candidates, and use reviewer scores as authoritative. Do not invent, adjust, or recompute scores.
8. Group expressions into a small number of families by mathematical structure, not by candidate id alone. For each family, identify members, typical formula pattern, performance range, strengths, weaknesses, and any clear evolution path.
9. Redraw a report-ready MCTS tree image:
   - Use `RUN_DIR/tree.png` and the textual output of `mcts.py tree` only as a scaffold.
   - Reconstruct the tree topology from `state.json`; never trust the raw tree's regex-extracted formula labels without checking each candidate's `ansatz.md`.
   - Replace wrong, missing, overlong, or ugly formula labels with verified concise labels. Prefer `candidate id + score + family label + short formula pattern`.
   - Make the figure readable in a Markdown/PDF report: adequate width, non-overlapping text, sensible line wrapping, and compact formula summaries.
   - Save the corrected figure as `RUN_DIR/tree_report.png`. The final report must use this corrected image, not the raw `tree.png`.
10. Write the final report to `RUN_DIR/report.md`. If `report.md` already exists, first copy it to `RUN_DIR/report.prev_YYMMDD_HHMM.md`.

## Report requirements

- Title the report `# AgonSR Report: <problem title or run name>`.
- The report must be self-contained enough to share with an outside reader. Explain the problem, variables, score meaning, best formula, metrics, and intuition without requiring access to proposer/reviewer internal notes.
- Never leave internal proposer-comparison language in the best-result or appendix sections. Avoid phrases such as "father", "grandfather", "sibling", "ancestor", "the proposer improved", or "the reviewer says" there.
- In the MCTS/family section, you may discuss search evolution and lineage using neutral language, for example `0001 → 0002 → 0013`.
- Include the corrected MCTS tree image in the family section with a relative Markdown link: `![MCTS tree](tree_report.png)`.
- Respect `USER_NOTES` for language, audience, emphasis, length, or formatting unless it conflicts with these instructions.
- Prefer concise, polished scientific writing over exhaustive transcript-like summaries.

## Required report structure

Use this structure unless `USER_NOTES` explicitly requests a compatible variation:

```markdown
# AgonSR Report: <problem title or run name>

## 1. Best result

### 1.1 Formula and performance

### 1.2 Intuition

### 1.3 Discussion

## 2. MCTS search landscape and expression families

![MCTS tree](tree_report.png)

### 2.1 Family overview

### 2.2 <family name>

...

## Appendix. Other top candidates

### Rank 2: <candidate id>

### Rank 3: <candidate id>

### Rank 4: <candidate id>

### Rank 5: <candidate id>
```

## Section guidance

### Best result

- Present the top-ranked candidate's formula, fitted constants, metrics, and final score.
- Explain the intuition independently, without relying on its relationship to earlier candidates.
- Put caveats, constraint checks, residual patterns, validation notes, robustness, and problem-specific miscellaneous points under `### 1.3 Discussion`.

### MCTS search landscape and expression families

- Start with a compact overview table listing family name, members, best member, score range, and main idea.
- Then describe each major family. Include:
  - representative formula pattern;
  - member list with scores;
  - key empirical behavior;
  - why it succeeded or failed;
  - clear evolution tree when present.
- Do not force every candidate into a large discussion. Minor one-off candidates may be grouped as "other explored forms" if appropriate.

### Appendix

- Include ranks 2 through 5 when available.
- For each candidate, include formula, score, important metrics if available, and standalone intuition.
- For each candidate, explicitly state which family from Section 2 it belongs to, using the same family name/label as the family overview.
- Do not describe these candidates as merely changes to their parents; write them as independent alternatives.

## Final response

After writing `RUN_DIR/report.md`, respond to the user with only:

- the resolved `RUN_DIR`;
- the report path;
- the corrected tree image path (`RUN_DIR/tree_report.png`);
- any important caveat, if one affected the report.
