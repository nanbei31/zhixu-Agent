"""Structured data shared by the CI parser, runner, and report writer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


def _tail(value: str, limit: int = 20000) -> str:
    if len(value) <= limit:
        return value
    return f"[... {len(value) - limit} earlier characters omitted ...]\n{value[-limit:]}"


@dataclass(frozen=True)
class CommandResult:
    command: str
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def combined_output(self) -> str:
        parts = [part.strip() for part in (self.stdout, self.stderr) if part.strip()]
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "timed_out": self.timed_out,
            "stdout": _tail(self.stdout),
            "stderr": _tail(self.stderr),
        }


@dataclass(frozen=True)
class PytestFailure:
    node_id: str
    file_path: str | None = None
    test_name: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class PytestSummary:
    failed: int = 0
    passed: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    duration_seconds: float | None = None
    failures: tuple[PytestFailure, ...] = ()
    locations: tuple[str, ...] = ()

    @property
    def headline(self) -> str:
        counts = []
        for count, label in (
            (self.failed, "failed"),
            (self.errors, "errors"),
            (self.passed, "passed"),
            (self.skipped, "skipped"),
        ):
            if count:
                counts.append(f"{count} {label}")
        return ", ".join(counts) if counts else "pytest result could not be summarized"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CiFixAttempt:
    number: int
    diagnosis: PytestSummary
    validation: CommandResult
    skill_name: str | None = None
    skill_loaded: bool = False
    context_summary: dict = field(default_factory=dict)
    agent_duration_seconds: float = 0.0
    usage: dict | None = None
    cost: dict | None = None

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "diagnosis": self.diagnosis.to_dict(),
            "validation": self.validation.to_dict(),
            "skill_name": self.skill_name,
            "skill_loaded": self.skill_loaded,
            "context_summary": self.context_summary,
            "agent_duration_seconds": round(self.agent_duration_seconds, 3),
            "usage": self.usage,
            "cost": self.cost,
        }


@dataclass
class CiFixReport:
    initial: CommandResult
    initial_summary: PytestSummary
    attempts: list[CiFixAttempt] = field(default_factory=list)
    workspace_policy: dict | None = None
    isolation: dict | None = None
    skill_name: str | None = None
    skill_loaded: bool = False
    run_id: str | None = None
    project_id: str | None = None
    usage: dict | None = None
    cost: dict | None = None
    timing: dict | None = None
    diff: dict | None = None

    @property
    def final_result(self) -> CommandResult:
        return self.attempts[-1].validation if self.attempts else self.initial

    @property
    def succeeded(self) -> bool:
        return self.final_result.succeeded

    @property
    def exit_code(self) -> int:
        return 0 if self.succeeded else 1

    def to_dict(self) -> dict:
        return {
            "schema_version": 5,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "succeeded": self.succeeded,
            "isolation": self.isolation,
            "skill_name": self.skill_name,
            "skill_loaded": self.skill_loaded,
            "workspace_policy": self.workspace_policy,
            "usage": self.usage,
            "cost": self.cost,
            "timing": self.timing,
            "diff": self.diff,
            "initial": self.initial.to_dict(),
            "initial_summary": self.initial_summary.to_dict(),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "final": self.final_result.to_dict(),
        }

    def render_text(self) -> str:
        lines = [
            "AutoCI-Fix report",
            f"  Command: {self.initial.command}",
            f"  Working directory: {self.initial.cwd}",
            (
                "  Repair Skill: "
                f"{self.skill_name or '(disabled)'} "
                f"({'loaded' if self.skill_loaded else 'not loaded'})"
            ),
            (
                "  Initial result: "
                f"{'passed' if self.initial.succeeded else 'failed'} "
                f"(exit {self.initial.exit_code}, {self.initial_summary.headline})"
            ),
        ]
        for attempt in self.attempts:
            result = attempt.validation
            state = "passed" if result.succeeded else "failed"
            lines.append(
                f"  Repair attempt {attempt.number}: {state} "
                f"(exit {result.exit_code}, {result.duration_seconds:.2f}s)"
            )
        if self.diff and self.diff.get("available"):
            lines.append(
                "  Diff: "
                f"{self.diff.get('changed_file_count', 0)} files "
                f"(+{self.diff.get('insertions', 0)}/-{self.diff.get('deletions', 0)})"
            )
        if self.usage:
            lines.append(
                "  Tokens: "
                f"{self.usage.get('input_tokens', 0)} input / "
                f"{self.usage.get('output_tokens', 0)} output"
            )
        if self.cost:
            lines.append(
                f"  Estimated cost: ${self.cost.get('estimated_usd', 0.0):.6f}"
            )
        if self.timing:
            lines.append(
                f"  Total duration: {self.timing.get('total_duration_seconds', 0.0):.2f}s"
            )
        lines.append(f"  Final result: {'PASSED' if self.succeeded else 'FAILED'}")
        return "\n".join(lines)
