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
├── 0001/
│   ├── ansatz.md               # main ansatz file
│   └── ...                     # other artifacts
└── 0002/
    ├── ansatz.md
    └── ...
```

## Ansatz file format

<ansatz template>

## One sentence

State the proposed ansatz in one sentence, using LaTeX syntax for the formula.

## Motivation and explanation

## Performance

Summarize the evaluation results, including key metrics.

## Artifacts

<review score="X">
...
</review>

</ansatz template>

The ansatz file body is written by the ansatz proposer; the review block is written by the ansatz reviewer.

## Dimensional consistency

Only add or subtract quantities with the same dimensions. Arguments of `exp`, `log`, `sin`, `cos`, and similar functions must be dimensionless. Both sides of an equation must have the same dimensions. Fitted scalar coefficients may carry units unless `PROBLEM_PATH` says they must be dimensionless.

## Expression complexity

When a problem asks for expression complexity, simplify the expression first and count tree leaves recursively.

```
LC(atom) = 1
LC(expr) = 1 + sum(LC(arg) for arg in expr.args)
```

Use the smallest LeafCount among equivalent simplified forms.

For $\sin^2(x)+\cos^2(x)+(x^2-1)/(x-1)$, `sympy.simplify` gives $x+2$, so the LeafCount is 3 instead of 20.

## File boundaries

Agents may modify only the current `WORKDIR`.

Do not modify:
- other candidates
- other runs
- ancestor ansatz files
- files outside `WORKDIR`
