# AutoSR

AutoSR is a Claude Code plugin for symbolic ansatz search.
Problems and generated artifacts live outside the plugin, usually in a problem workspace such as `artifacts/pile_efficiency/`.

## Start Claude Code with the plugin

From a problem workspace:

```
cd /path/to/problem-workspace
claude --plugin-dir /path/to/AutoSR/autosr
```

Then run `/llm-mcts 20 problem.md`.

Resume an existing run:

```
/llm-mcts 20 problem.md --resume runs/llm-mcts_YYMMDD_HHMM
```

## `problem.md`

`problem.md` should contain the actual problem definition: objective, variables, data/docs/scripts to read, constraints, evaluation method, scoring rubric, and expected report contents. Use paths relative to the problem workspace.

## `IGNOREME.md`

Optional. Put special per-role notes here when they should not live in the general problem statement. Format:

```
## Notes to ansatz-proposer

...

## Notes to ansatz-reviewer

...
```

The dispatcher reads this file and passes each section only to the corresponding subagent.

## Pipeline

`llm-mcts` is only a dispatcher. It initializes or resumes a run, asks `mcts.py next` for the next candidate, sends fixed minimal prompts to `ansatz-proposer` and `ansatz-reviewer`, reads the `<review score="X">` block from `report.md`, calls `mcts.py update`, and finally shows the best candidates.

Run files are written under `runs/`. See `autosr/references/project_manual.md` for the exact workspace and report conventions.
