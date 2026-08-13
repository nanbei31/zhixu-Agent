# Pytest Repair Benchmark

Mini Claude includes a deterministic suite of 40 Python repair tasks. It uses
the existing AutoCI-Fix Worktree, Skill, WorkspacePolicy, Diff, Token, timing,
and cost pipeline, then adds hidden-test and changed-file scoring.

## Dataset

The built-in `pytest-repair-40` suite has eight categories with five cases each:

| Category | Cases | Main coverage |
|---|---:|---|
| `arithmetic` | 5 | operators, division, averages, clamping, percentages |
| `strings` | 5 | normalization, formatting, masking, joining |
| `collections` | 5 | order, sorting, merging, counting, chunking |
| `boundaries` | 5 | offsets, thresholds, zero, inclusive ends, retries |
| `datetime` | 5 | weekdays, distances, parsing, expiry, month ends |
| `multifile` | 5 | business logic across models, config, and services |
| `exceptions` | 5 | error propagation, exception type, validation |
| `async-resources` | 5 | await, gather, context cleanup, generators, defaults |

Difficulty distribution: 20 easy, 16 medium, and 4 hard tasks.

Each YAML case defines broken production files, a public test shown to the
Agent, a hidden test used only by the evaluator, writable files, and an oracle
solution used to validate the dataset. The oracle is never included in a model
run. Case tests use Python's `-B` mode so stale bytecode cannot turn a correct
first repair into a false retry.

## Validate Fixtures

Validation does not call a model and does not require an API key. For every
case it proves that the public test fails before repair and that the oracle
passes both public and hidden tests:

```bash
mini-claude-py --benchmark --benchmark-validate
```

## Run Experiments

Start with one case to confirm model and API settings:

```bash
mini-claude-py \
  --benchmark \
  --benchmark-case arithmetic-wrong-operator \
  --max-fix-attempts 2
```

Then run a category or the complete suite:

```bash
mini-claude-py --benchmark --benchmark-category collections
mini-claude-py --benchmark
```

Model output is nondeterministic. A more meaningful comparison repeats every
case three times and stores reports in an explicit experiment directory:

```bash
mini-claude-py \
  --benchmark \
  --benchmark-repetitions 3 \
  --benchmark-output benchmark-results/baseline
```

Useful selection controls:

```text
--benchmark-case ID          repeatable exact case selection
--benchmark-category NAME    repeatable category selection
--benchmark-limit N          first N cases after filtering
--benchmark-repetitions N    independent runs per case
--benchmark-suite PATH       alternate suite YAML
```

## Scoring

A case passes only when all four conditions hold:

1. AutoCI's public test passes.
2. The generated Git patch applies to a clean fixture.
3. Public and hidden tests pass after applying the patch.
4. Every changed file is listed in `allowed_changes`.

The aggregate report includes final success rate, Success@1, hidden-test pass
rate, policy compliance, input/output Tokens, estimated cost, and average
duration. `benchmark-report.json` is intended for automated comparison;
`benchmark-results.csv` is convenient for spreadsheets and charts. Individual
case evidence remains under `autoci-runs/` with the normal `report.json`,
`changes.patch`, `events.jsonl`, logs, and SQLite index.

Estimated cost uses Mini Claude's static pricing table and is not a provider
invoice. Keep the model name, backend, Skill version, attempts, and repetition
count fixed when comparing experiments.
