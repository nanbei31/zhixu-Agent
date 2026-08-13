# GitHub Actions AutoCI Repair

The `AutoCI Repair` workflow turns an isolated Mini Claude repair into a Draft
pull request without giving the Agent repository write permission.

## Security Model

The workflow is deliberately split into three jobs:

| Job | Repository permission | Model secret | Runs patched code |
|---|---|---|---|
| `repair` | `contents: read` | Yes, Agent step only | Yes, without common cloud/API credentials |
| `verify` | `contents: read` | No | Yes |
| `publish` | `contents: write`, `pull-requests: write` | No | No |

`repair` uses the existing detached Worktree and produces an evidence artifact.
`verify` checks out the immutable base SHA, validates and applies the patch, and
runs the requested tests independently. `publish` checks out the same SHA,
renders the PR body before applying the patch, revalidates the exact artifact,
and creates a Draft PR. It never installs dependencies or executes patched
project code.

The publishing gate rejects:

- unsuccessful or malformed AutoCI reports;
- base commit, Patch SHA256, file list, or line-count mismatches;
- files outside the checked-in WorkspacePolicy writable roots;
- tests, workflows, Skills, the publishing gate itself, and dependency files;
- deletions, symlinks, binary changes, oversized patches, and empty patches.

These rules are configured in `.claude/settings.json` under `githubAutoFix`.
CLI inputs cannot widen WorkspacePolicy.

## Repository Setup

In GitHub, open **Settings > Secrets and variables > Actions** and configure one
provider:

```text
ANTHROPIC_API_KEY
ANTHROPIC_BASE_URL       optional
```

or:

```text
OPENAI_API_KEY
OPENAI_BASE_URL          required for a non-default compatible endpoint
```

Optionally add an Actions variable:

```text
MINI_CLAUDE_MODEL
```

Open **Settings > Actions > General > Workflow permissions** and enable:

```text
Read and write permissions
Allow GitHub Actions to create and approve pull requests
```

The workflow still declares least-privilege permissions per job. Protect the
default branch, require normal CI and human review, and add `.github/` plus
`.claude/` to `CODEOWNERS` before using automatic publishing.

## First Dry Run

The workflow is manual and `dry_run` defaults to `true`. In the Actions page,
select **AutoCI Repair**, choose **Run workflow**, and supply a known failing
test command and a production-code target.

The equivalent GitHub CLI command is:

```bash
gh workflow run autoci-repair.yml \
  -f base_ref=main \
  -f setup_command='python -m pip install ./python pytest' \
  -f test_command='python -m pytest -q python/tests/test_ci_runner.py' \
  -f target='python/mini_claude/ci' \
  -f max_attempts=2 \
  -f dry_run=true
```

Inspect both downloadable artifacts:

```text
autoci-repair-<run-id>-<attempt>     Agent report, patch, events, and logs
autoci-verified-<run-id>-<attempt>   the same evidence plus independent validation
```

## Create a Draft PR

After a dry run succeeds, rerun with:

```bash
gh workflow run autoci-repair.yml \
  -f base_ref=main \
  -f setup_command='python -m pip install ./python pytest' \
  -f test_command='python -m pytest -q python/tests/test_ci_runner.py' \
  -f target='python/mini_claude/ci' \
  -f max_attempts=2 \
  -f dry_run=false
```

The resulting branch is named `autoci/fix-<run-id>-<attempt>`. The PR body
contains test outcomes, changed files, Diff size, Token usage, estimated cost,
duration, Skill name, Patch SHA256, policy scope, and an Actions evidence link.

## Trust Boundary

Only repository maintainers should dispatch this workflow. `setup_command`,
`test_command`, `target`, and `base_ref` are trusted maintainer inputs. Do not
connect this workflow directly to `pull_request_target`, a public issue comment,
or a failed Fork workflow. GitHub warns that privileged `workflow_run` and
`pull_request_target` jobs can expose write tokens or secrets when they check
out untrusted content.

The first automatic trigger, if added later, should accept only failures on a
protected branch at an immutable SHA. Fork pull requests must remain outside
the secret-bearing repair path.
