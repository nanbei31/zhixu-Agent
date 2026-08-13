# AutoCI-Fix Python Demo

This fixture contains an intentional production-code bug. Run it from this
directory so `calculator_checks.py` can import `calculator.py`:

```bash
cd examples/ci-fix-python
mini-claude-py \
  --fix-ci \
  --test-command "python -m pytest -q calculator_checks.py" \
  --target calculator.py \
  --max-fix-attempts 2 \
  --ci-report ci-report.json
```

The repository's `.claude/settings.json` permanently allows writes in this
demo directory, so `--allowed-path` is not required. Supplying it can only
further narrow the configured writable paths.

AutoCI-Fix should parse the failing pytest log, ask the Agent to repair
`calculator.py`, rerun the same command, and finish with `Final result: PASSED`.

The command prints the run artifact directory. Inspect `report.json` for the
Diff, Token, estimated cost, and duration summary; inspect `events.jsonl` for
the ordered execution trace; and apply `changes.patch` only after review.
`mini-claude-py runs` lists recent artifact paths and `mini-claude-py usage`
shows aggregate usage without requiring an API key.

The demo modifies `calculator.py`. Restore its intentional subtraction bug
before running the demonstration again.
