"""Structured repair context shared by CI prompting and reporting."""

from __future__ import annotations

from dataclasses import dataclass

from .models import CommandResult, PytestSummary

MAX_SUMMARY_ITEMS = 20
MAX_RENDERED_ITEMS = 30


def _bounded_log(output: str, limit: int) -> str:
    if len(output) <= limit:
        return output
    half = max(1, limit // 2)
    omitted = len(output) - half * 2
    return (
        f"{output[:half]}\n\n"
        f"[... {omitted} log characters omitted ...]\n\n"
        f"{output[-half:]}"
    )


def classify_failure(result: CommandResult, summary: PytestSummary) -> str:
    if result.timed_out:
        return "timeout"
    output = result.combined_output.lower()
    if any(value in output for value in ("command not found", "permission denied", "no module named pytest")):
        return "environment"
    if any(value in output for value in ("error collecting", "collection error", "during collection")):
        return "collection"
    if any(value in output for value in ("importerror", "modulenotfounderror", "cannot import name")):
        return "import"
    if "fixture" in output and any(value in output for value in ("not found", "scope", "lookup")):
        return "fixture"
    if summary.failed or "assertionerror" in output:
        return "assertion"
    return "unknown"


@dataclass(frozen=True)
class RepairContext:
    attempt: int
    max_attempts: int
    test_command: str
    result: CommandResult
    summary: PytestSummary
    targets: tuple[str, ...] = ()
    writable_paths: tuple[str, ...] = ()
    previous_attempts: tuple[dict, ...] = ()

    @property
    def classification(self) -> str:
        return classify_failure(self.result, self.summary)

    def summary_dict(self) -> dict:
        failing_tests = [failure.node_id for failure in self.summary.failures]
        locations = list(self.summary.locations)
        return {
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "classification": self.classification,
            "pytest_headline": self.summary.headline,
            "exit_code": self.result.exit_code,
            "timed_out": self.result.timed_out,
            "failing_tests": failing_tests[:MAX_SUMMARY_ITEMS],
            "failing_test_count": len(failing_tests),
            "traceback_locations": locations[:MAX_SUMMARY_ITEMS],
            "traceback_location_count": len(locations),
            "targets": list(self.targets),
            "writable_paths": list(self.writable_paths),
            "raw_log_characters": len(self.result.combined_output),
            "previous_attempts": list(self.previous_attempts),
        }

    def render(self, *, log_limit: int) -> str:
        targets = "\n".join(f"- {value}" for value in self.targets)
        if not targets:
            targets = "- 未显式指定目标，请根据失败信息定位相关文件。"

        writable = "\n".join(f"- {value}" for value in self.writable_paths)
        if not writable:
            writable = "- 未配置可写路径。"

        failures = []
        for failure in self.summary.failures[:MAX_RENDERED_ITEMS]:
            detail = f": {failure.message}" if failure.message else ""
            failures.append(f"- {failure.node_id}{detail}")
        if len(self.summary.failures) > MAX_RENDERED_ITEMS:
            failures.append(
                f"- [... 省略 {len(self.summary.failures) - MAX_RENDERED_ITEMS} 个失败测试 ...]"
            )
        failure_text = "\n".join(failures) or "- 未解析出 FAILED node ID，请检查原始日志。"

        rendered_locations = [
            f"- {value}" for value in self.summary.locations[:MAX_RENDERED_ITEMS]
        ]
        if len(self.summary.locations) > MAX_RENDERED_ITEMS:
            rendered_locations.append(
                f"- [... 省略 {len(self.summary.locations) - MAX_RENDERED_ITEMS} 个位置 ...]"
            )
        locations = "\n".join(rendered_locations)
        if not locations:
            locations = "- 未解析出 Python traceback 位置。"

        history = []
        for item in self.previous_attempts:
            history.append(
                f"- 第 {item['number']} 轮：exit={item['exit_code']}，"
                f"result={item['result']}"
            )
        history_text = "\n".join(history) or "- 这是第一轮修复。"

        log = _bounded_log(self.result.combined_output, log_limit)
        return f"""## AutoCI-Fix 结构化修复上下文

### 执行信息

- 工作目录：{self.result.cwd}
- 测试命令：{self.test_command}
- 当前轮次：{self.attempt}/{self.max_attempts}
- 失败分类：{self.classification}
- 解析结果：{self.summary.headline}
- 退出码：{self.result.exit_code}
- 是否超时：{self.result.timed_out}

### 主要目标（仅用于定位，不授予权限）

{targets}

### 运行时强制可写路径

{writable}

### 失败测试

{failure_text}

### Traceback 位置

{locations}

### 之前的修复尝试

{history_text}

### 原始 pytest 输出

```text
{log}
```"""
