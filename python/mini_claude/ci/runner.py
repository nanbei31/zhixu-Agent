"""Run a failing test command, ask the Agent to repair it, and verify."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..skills import SkillDefinition, resolve_skill_prompt
from .context import RepairContext
from .models import CiFixAttempt, CiFixReport, CommandResult, PytestSummary
from .pytest_parser import parse_pytest_output


class RepairAgent(Protocol):
    async def chat(self, user_message: str) -> None: ...


CommandRunner = Callable[[str, Path, float], CommandResult]
EventSink = Callable[[str, dict], None]
SENSITIVE_TEST_ENV_NAMES = frozenset({
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_CLIENT_SECRET",
    "GOOGLE_APPLICATION_CREDENTIALS",
})
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "total_accounted_tokens",
    "turns",
)


@dataclass(frozen=True)
class CiFixConfig:
    test_command: str = "python -m pytest -q"
    cwd: Path = Path(".")
    max_attempts: int = 2
    timeout_seconds: float = 300.0
    log_limit: int = 16000
    targets: tuple[str, ...] = ()
    writable_paths: tuple[str, ...] = ()
    workspace_policy: dict | None = None
    repair_skill_name: str | None = "pytest-repair"

    def __post_init__(self) -> None:
        if not self.test_command.strip():
            raise ValueError("test_command must not be empty")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _usage_snapshot(agent: RepairAgent) -> dict | None:
    getter = getattr(agent, "get_usage_metrics", None)
    return getter() if callable(getter) else None


def _usage_delta(before: dict | None, after: dict | None) -> tuple[dict | None, dict | None]:
    if before is None or after is None:
        return None, None
    usage = {
        field: max(0, int(after.get(field, 0)) - int(before.get(field, 0)))
        for field in TOKEN_FIELDS
    }
    usage["model"] = after.get("model")
    usage["provider"] = after.get("provider")
    cost = {
        "estimated_usd": max(
            0.0,
            float(after.get("estimated_cost_usd", 0.0))
            - float(before.get("estimated_cost_usd", 0.0)),
        ),
        "currency": "USD",
        "is_estimate": bool(after.get("cost_is_estimate", True)),
        "model": after.get("model"),
        "pricing_source": after.get("pricing_source"),
    }
    return usage, cost


def _emit(event_sink: EventSink | None, event_type: str, **data) -> None:
    if event_sink is not None:
        event_sink(event_type, data)


def run_test_command(command: str, cwd: Path, timeout_seconds: float) -> CommandResult:
    """Run a user-supplied CI command and capture a deterministic result."""
    resolved_cwd = cwd.resolve()
    started = time.monotonic()
    test_env = {
        key: value for key, value in os.environ.items()
        if key not in SENSITIVE_TEST_ENV_NAMES
    }
    # A same-size repair written within the source file's timestamp resolution can
    # otherwise reuse bytecode produced by the initial failing test.
    test_env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=resolved_cwd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=test_env,
            timeout=timeout_seconds,
        )
        return CommandResult(
            command=command,
            cwd=str(resolved_cwd),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command=command,
            cwd=str(resolved_cwd),
            exit_code=124,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )


def build_repair_prompt(
    config: CiFixConfig,
    result: CommandResult,
    summary: PytestSummary,
    attempt: int,
    repair_skill: SkillDefinition | None = None,
) -> str:
    context = RepairContext(
        attempt=attempt,
        max_attempts=config.max_attempts,
        test_command=config.test_command,
        result=result,
        summary=summary,
        targets=config.targets,
        writable_paths=config.writable_paths,
    )
    return _render_repair_prompt(context, repair_skill, log_limit=config.log_limit)


def _render_repair_prompt(
    context: RepairContext,
    skill: SkillDefinition | None,
    *,
    log_limit: int,
) -> str:
    rendered_context = context.render(log_limit=log_limit)
    if skill is not None:
        prompt = resolve_skill_prompt(skill, rendered_context)
    else:
        prompt = (
            "You are repairing a failing local CI run. Diagnose the root cause, "
            "read the relevant implementation and tests, and make the smallest "
            f"safe production-code change.\n\n{rendered_context}"
        )
    return f"{prompt}\n\n{_runner_contract()}"


def _runner_contract() -> str:
    return """Runner contract:
- Do not commit, push, install dependencies, or modify CI configuration.
- Do not run the test command; AutoCI-Fix performs validation.
- WorkspacePolicy is the final authority for every tool call."""


async def run_ci_fix(
    agent: RepairAgent,
    config: CiFixConfig,
    command_runner: CommandRunner = run_test_command,
    *,
    repair_skill: SkillDefinition | None = None,
    event_sink: EventSink | None = None,
) -> CiFixReport:
    if (
        repair_skill is not None
        and config.repair_skill_name is not None
        and repair_skill.name != config.repair_skill_name
    ):
        raise ValueError(
            f"loaded Skill {repair_skill.name!r} does not match configured "
            f"repair Skill {config.repair_skill_name!r}"
        )
    _emit(event_sink, "test_started", phase="initial", command=config.test_command)
    current = command_runner(
        config.test_command,
        config.cwd,
        config.timeout_seconds,
    )
    _emit(
        event_sink,
        "test_finished",
        phase="initial",
        exit_code=current.exit_code,
        duration_seconds=current.duration_seconds,
        timed_out=current.timed_out,
    )
    summary = parse_pytest_output(current.combined_output)
    report = CiFixReport(
        initial=current,
        initial_summary=summary,
        workspace_policy=config.workspace_policy,
        skill_name=config.repair_skill_name,
        skill_loaded=repair_skill is not None,
    )

    if current.succeeded:
        report.timing = {
            "test_duration_seconds": round(current.duration_seconds, 3),
            "agent_duration_seconds": 0.0,
        }
        return report

    for attempt_number in range(1, config.max_attempts + 1):
        previous_attempts = tuple(
            {
                "number": attempt.number,
                "exit_code": attempt.validation.exit_code,
                "result": "passed" if attempt.validation.succeeded else "failed",
            }
            for attempt in report.attempts
        )
        context = RepairContext(
            attempt=attempt_number,
            max_attempts=config.max_attempts,
            test_command=config.test_command,
            result=current,
            summary=summary,
            targets=config.targets,
            writable_paths=config.writable_paths,
            previous_attempts=previous_attempts,
        )
        prompt = _render_repair_prompt(
            context,
            repair_skill,
            log_limit=config.log_limit,
        )
        usage_before = _usage_snapshot(agent)
        agent_started = time.monotonic()
        _emit(
            event_sink,
            "agent_attempt_started",
            attempt=attempt_number,
            skill_name=config.repair_skill_name,
            context_summary=context.summary_dict(),
        )
        await agent.chat(prompt)
        agent_duration = time.monotonic() - agent_started
        usage_after = _usage_snapshot(agent)
        attempt_usage, attempt_cost = _usage_delta(usage_before, usage_after)
        _emit(
            event_sink,
            "agent_attempt_finished",
            attempt=attempt_number,
            duration_seconds=agent_duration,
            usage=attempt_usage,
            cost=attempt_cost,
        )
        _emit(
            event_sink,
            "test_started",
            phase="validation",
            attempt=attempt_number,
            command=config.test_command,
        )
        validation = command_runner(
            config.test_command,
            config.cwd,
            config.timeout_seconds,
        )
        _emit(
            event_sink,
            "test_finished",
            phase="validation",
            attempt=attempt_number,
            exit_code=validation.exit_code,
            duration_seconds=validation.duration_seconds,
            timed_out=validation.timed_out,
        )
        report.attempts.append(
            CiFixAttempt(
                number=attempt_number,
                diagnosis=summary,
                validation=validation,
                skill_name=config.repair_skill_name,
                skill_loaded=repair_skill is not None,
                context_summary=context.summary_dict(),
                agent_duration_seconds=agent_duration,
                usage=attempt_usage,
                cost=attempt_cost,
            )
        )
        if validation.succeeded:
            break
        current = validation
        summary = parse_pytest_output(current.combined_output)

    test_duration = report.initial.duration_seconds + sum(
        attempt.validation.duration_seconds for attempt in report.attempts
    )
    agent_duration = sum(
        attempt.agent_duration_seconds for attempt in report.attempts
    )
    report.timing = {
        "test_duration_seconds": round(test_duration, 3),
        "agent_duration_seconds": round(agent_duration, 3),
    }
    return report
