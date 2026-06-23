# AutoSR Manual

## Problem workspace layout

```
problem-workspace/
├── .venv                       # shared venv for the problem; use Python 3.13 unless specified otherwise
├── problem.md
├── ...                         # data, scripts, docs, etc. as referenced by problem.md
└── runs/
    ├── llm-mcts_YYMMDD_HHMM/
    └── llm-sr_YYMMDD_HHMM/
```

## Run layout

```
runs/llm-mcts_YYMMDD_HHMM/
├── state.json
└── candidates/
    ├── 0001/
    │   ├── report.md           # main report
    │   └── ...                 # other artifacts
    └── 0002/
        ├── report.md
        └── ...
```

## Report format

<report template>

## One sentence

State the proposed ansatz in one sentence, using LaTeX syntax for the formula.

## Motivation and explanation

## Performance

Summarize the evaluation results, including key metrics.

## Artifacts

<review score="X">
...
</review>

</report template>

Higher score is better. The report body is written by the ansatz proposer; the review block is written by the ansatz reviewer.

## File boundaries

Agents may modify only the current `WORKDIR`.

Do not modify:
- other candidates
- other runs
- ancestor reports
- files outside `WORKDIR`
