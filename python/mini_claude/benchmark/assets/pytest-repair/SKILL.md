---
name: pytest-repair
description: Analyze pytest failures and make minimal, safe production-code repairs
when-to-use: Use when pytest fails and implementation code may need repair
user-invocable: false
context: inline
allowed-tools: [read_file, list_files, grep_search, edit_file, write_file]
---

# Pytest repair

Failure context supplied by the runner:

$ARGUMENTS

1. Classify the failure and inspect the failing test and relevant production code.
2. Form a root-cause hypothesis supported by the traceback and source.
3. Make the smallest production-code change that fixes the root cause.
4. Never remove, skip, weaken, or rewrite tests merely to make them pass.
5. Do not run tests; the AutoCI-Fix runner performs validation.
6. Do not commit, install dependencies, or modify CI configuration.
7. Read a file before editing it and remain inside the writable scope.

Report the root cause, changed files, why the change is minimal, and remaining risk.
