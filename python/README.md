# Mini Claude Code — Python 版

与 TypeScript 版功能 99% 一致的 Python 实现。**需要 Python >= 3.11**。

> 📖 完整教程文档见 [claude-code-from-scratch](https://github.com/Windy3f3f3f3f/claude-code-from-scratch)（文档中所有代码块均支持 TypeScript / Python 切换）

## 快速开始

```bash
# 安装（需要 Python 3.11+）
cd python
pip install -e .

# 设置 API Key
export ANTHROPIC_API_KEY=sk-ant-...

# 运行
mini-claude-py "hello"               # 一次性模式
mini-claude-py                       # 交互式 REPL
mini-claude-py --yolo "list files"   # 跳过确认
mini-claude-py --plan "refactor this" # 计划模式
python -m mini_claude "hello"        # 也可以用 python -m 方式运行

# 使用 OpenAI 兼容后端
OPENAI_API_KEY=sk-xxx mini-claude-py --api-base https://api.openai.com/v1 --model gpt-4o "hello"
```

## AutoCI-Fix

AutoCI-Fix runs a pytest command, parses failures, asks the Agent to make a
minimal repair, and reruns the same command for verification:

```bash
cd /path/to/your/python-project

mini-claude-py \
  --fix-ci \
  --test-command "python -m pytest -q" \
  --max-fix-attempts 2 \
  --ci-timeout 300 \
  --ci-report ci-report.json
```

The default permission mode for `--fix-ci` automatically approves file edits.
Dangerous shell commands remain subject to the normal permission checks. The
process exits with code `0` when the final test run passes, `1` when failures
remain, and `2` when AutoCI-Fix itself cannot run.

### Git worktree isolation and rollback

`--fix-ci` runs in a detached Git worktree by default. The source checkout must
be clean, `HEAD` must exist, and `.claude/settings.json` must be tracked. All
repair attempts accumulate in the same temporary worktree, so a later attempt
can build on earlier edits without changing the checkout where the command was
started.

Every run writes evidence outside the source checkout. The default location is
the operating system's per-user application data directory, grouped by a
stable project ID and run ID:

```text
Windows: %LOCALAPPDATA%\MiniClaude\runs\<project-id>\<run-id>\
Linux:   ${XDG_DATA_HOME:-~/.local/share}/mini-claude/runs/<project-id>/<run-id>/
macOS:   ~/Library/Application Support/MiniClaude/runs/<project-id>/<run-id>/
```

Set `MINI_CLAUDE_DATA_DIR` to override the local data root. For CI, use
`--artifacts-dir PATH` to place the run directory and its `autoci.db` index in
a job artifact directory.

```text
report.json       structured result and effective workspace policy
changes.patch     reviewable binary-safe Git patch, including new files
final-test.log    stdout and stderr from the final validation
git-status.txt    final changed-file list
metadata.json     base commit, paths, and rollback state
events.jsonl      ordered lifecycle, test, Agent, usage, and Diff events
```

`report.json` schema version 5 records the changed-file Diff summary, per-attempt
and total Token usage, estimated USD cost, test/Agent/total duration, model,
provider, Skill, and isolation metadata. `changes.patch` remains the source of
truth for reviewing or applying the exact code change. Cost is explicitly an
estimate based on Mini Claude's static pricing table; provider billing is the
authoritative amount.

The worktree is removed after a successful run because the patch is the review
artifact. When all attempts fail, the same removal is the rollback: cumulative
edits disappear while the patch and logs remain available. Use
`--keep-failed-worktree` to preserve a failed workspace for debugging, or
`--no-isolate` only when intentionally debugging against the current checkout.
The local data root also contains `autoci.db`, a lightweight SQLite index for
cross-run comparison. It stores summaries and artifact paths, while full logs
and prompts stay in the per-run files. View it without configuring an API key:

```bash
mini-claude-py runs       # recent runs and their artifact directories
mini-claude-py usage      # cumulative runs, pass rate, Tokens, cost, and timing
```

The same views are available as `/runs` and `/usage` in the interactive REPL.

### 40-case repair benchmark

The built-in `pytest-repair-40` Benchmark covers arithmetic, strings,
collections, boundaries, datetime, multi-file business logic, exceptions, and
async/resource management. Every task has a public failing test, evaluator-only
hidden tests, an explicit writable scope, and an oracle solution used solely to
validate the fixture.

```bash
# No API key required: prove all 40 fixtures fail before repair and pass with the oracle
mini-claude-py --benchmark --benchmark-validate

# Control cost with one case, then run the full experiment
mini-claude-py --benchmark --benchmark-case arithmetic-wrong-operator
mini-claude-py --benchmark --benchmark-repetitions 3
```

A case counts as successful only when AutoCI passes, its patch applies to a
clean fixture, hidden tests pass, and the Diff contains only allowed production
files. Reports include Success@1, final success rate, hidden-test pass rate,
policy compliance, Token, estimated cost, and duration. See
[`BENCHMARK.md`](./BENCHMARK.md) for case categories, command options, metric
definitions, and experiment guidance.

### GitHub Actions Draft PR repair

`.github/workflows/autoci-repair.yml` provides a manual, three-job repair flow.
The Agent has read-only repository access, independent verification has no
model secret, and only the final publishing job can push a previously verified
Patch. Draft PR publishing is disabled by default through the `dry_run` input.

The deterministic gate checks the immutable base commit, Patch SHA256, Diff
metadata, WorkspacePolicy, protected paths, file types, and independent test
result before publishing. See
[`docs/github-actions-autofix.md`](../docs/github-actions-autofix.md) for GitHub
Secrets, repository permissions, commands, artifacts, and the trust boundary.

```bash
mini-claude-py \
  --fix-ci \
  --test-command "pytest -q tests/test_order.py" \
  --target src/order_service.py \
  --repair-skill pytest-repair

# Review or apply a successful repair from the source checkout.
# Use the artifact path printed by AutoCI-Fix or `mini-claude-py runs`:
git apply --check /path/to/<run-id>/changes.patch
git apply /path/to/<run-id>/changes.patch
```

### Persistent workspace policy

AutoCI-Fix requires `.claude/settings.json` to define a hard project boundary.
`--target` only prioritizes context; it never grants write permission. The
optional `--allowed-path` can further narrow configured writable paths, but it
cannot widen them.

```json
{
  "workspacePolicy": {
    "readablePaths": ["."],
    "writablePaths": ["src/", "python/mini_claude/", "examples/ci-fix-python/"],
    "denyPaths": [".git/", ".env", ".env.*", "**/*.pem", "**/*.key"],
    "agentTools": ["read_file", "list_files", "grep_search", "edit_file", "write_file"],
    "allowAgentShell": false
  }
}
```

The project root is inferred from the directory containing `.claude`; no
machine-specific absolute path is committed. A missing policy always fails
closed.

```bash
mini-claude-py \
  --fix-ci \
  --test-command "python -m pytest -q calculator_checks.py" \
  --target calculator.py
```

The runtime resolves `..` and symbolic links, denies sensitive paths before
all normal permission modes (including `--yolo`), disables model-generated
shell commands for AutoCI-Fix, and records the effective policy in its JSON
report.

### Skills

Project Skills live at `.claude/skills/<name>/SKILL.md`. Skill frontmatter is
parsed with `yaml.safe_load` and validated against a JSON Schema before the
prompt is exposed to the Agent. Invalid YAML, unknown fields, invalid context
values, and malformed `allowed-tools` lists fail with an explicit error.
Both `user-invocable` and legacy `user_invocable` are accepted. YAML booleans
and trimmed, case-insensitive string values such as `" FALSE "` are normalized
before Schema validation.

```yaml
---
name: pytest-repair
description: 分析 pytest 失败并进行最小、安全的生产代码修复
when-to-use: 当 pytest 命令失败且可能需要修改实现代码时使用
user-invocable: true
context: inline
allowed-tools: [read_file, list_files, grep_search, edit_file, write_file]
---
```

`allowed-tools` is enforced at runtime for both manually invoked inline Skills
and model-invoked Skills. It is intersected with `WorkspacePolicy`: a Skill can
further restrict an Agent, but can never grant access forbidden by project
policy. Project Skill discovery walks up from the invocation directory, so the
same checked-in Skill is available inside nested Git Worktree directories.

After an isolated worktree is created, `workflow.py` explicitly loads
`pytest-repair` from that worktree once before the repair loop. `runner.py`
resolves its `$ARGUMENTS` and sends the resulting prompt through repeated
`agent.chat()` calls on the same Agent. The model is not given the `skill` tool
in AutoCI mode; file authority remains exclusively with `WorkspacePolicy`.
`ci/context.py` supplies structured arguments
instead of an ad-hoc log prompt: classification, pytest summary, failing node
IDs, traceback locations, targets, writable roots, prior attempts, timeout and
exit state, plus bounded raw evidence. Reports record `skill_name`,
`skill_loaded`, and a compact `context_summary` for every attempt.

Interactive REPL mode still exposes registered Skills. Each inline Skill's
runtime tool scope is cleared at the end of its turn, so the same long-lived
Agent can invoke another Skill on later input without leaking the old scope.

## 文件结构

| Python 文件 | 对应 TypeScript | 说明 |
|-------------|----------------|------|
| `agent.py` | `agent.ts` | Agent 核心循环、双后端、4 层压缩 |
| `tools.py` | `tools.ts` | 10 个工具 + 5 种权限模式 |
| `__main__.py` | `cli.ts` | CLI 入口与 REPL |
| `ui.py` | `ui.ts` | 终端 UI（rich） |
| `prompt.py` | `prompt.ts` | 系统提示词构造 |
| `session.py` | `session.ts` | 会话管理 |
| `memory.py` | `memory.ts` | 记忆系统 |
| `skills.py` | `skills.ts` | 技能系统 |
| `subagent.py` | `subagent.ts` | 子 Agent |
| `frontmatter.py` | `frontmatter.ts` | YAML frontmatter 解析 |
| `ci/` | — | pytest 日志解析、自动修复编排与报告 |

## 依赖

- `anthropic` — Anthropic SDK（流式）
- `openai` — OpenAI SDK（兼容后端）
- `rich` — 终端彩色输出
