"""High-level AutoCI-Fix workflow with optional Git worktree isolation."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Protocol

from ..skills import get_skill_by_name
from ..workspace_policy import WorkspacePolicy, load_workspace_policy
from .models import CiFixReport
from .runner import CiFixConfig, RepairAgent, run_ci_fix
from .storage import (
    EventRecorder,
    create_run_paths,
    generate_run_id,
    get_database_path,
    index_run,
    project_id,
    write_run_artifacts,
)
from .worktree import WorktreeSession, WorktreeSnapshot


class ManagedRepairAgent(RepairAgent, Protocol):
    async def close(self) -> None: ...


AgentFactory = Callable[[WorkspacePolicy], ManagedRepairAgent]


@dataclass(frozen=True)
class CiWorkflowResult:
    report: CiFixReport
    artifact_dir: Path | None = None
    worktree_root: Path | None = None
    worktree_preserved: bool = False


@contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


async def _execute(
    *,
    execution_cwd: Path,
    config: CiFixConfig,
    agent_factory: AgentFactory,
    cli_allowed_paths: tuple[str, ...],
    targets: tuple[str, ...],
    events: EventRecorder,
) -> CiFixReport:
    with _working_directory(execution_cwd):
        policy = load_workspace_policy(
            execution_cwd,
            cli_allowed_paths=cli_allowed_paths,
            targets=targets,
        )
        events.emit("workspace_policy_loaded", policy.to_dict())
        effective_config = replace(
            config,
            cwd=execution_cwd,
            targets=policy.relative_targets(),
            writable_paths=policy.relative_writable_roots(),
            workspace_policy=policy.to_dict(),
        )
        repair_skill = (
            get_skill_by_name(effective_config.repair_skill_name)
            if effective_config.repair_skill_name
            else None
        )
        events.emit(
            "skill_loaded",
            {
                "skill_name": effective_config.repair_skill_name,
                "loaded": repair_skill is not None,
                "source": repair_skill.source if repair_skill else None,
            },
        )
        agent = agent_factory(policy)
        events.emit("agent_created", {"working_directory": str(execution_cwd)})
        try:
            report = await run_ci_fix(
                agent,
                effective_config,
                repair_skill=repair_skill,
                event_sink=events,
            )
            usage_getter = getattr(agent, "get_usage_metrics", None)
            if callable(usage_getter):
                metrics = usage_getter()
                report.usage = {
                    key: value for key, value in metrics.items()
                    if key not in {"estimated_cost_usd", "cost_is_estimate", "pricing_source"}
                }
                report.cost = {
                    "estimated_usd": float(metrics.get("estimated_cost_usd", 0.0)),
                    "currency": "USD",
                    "is_estimate": bool(metrics.get("cost_is_estimate", True)),
                    "model": metrics.get("model"),
                    "pricing_source": metrics.get("pricing_source"),
                }
            return report
        finally:
            await agent.close()
            events.emit("agent_closed")


def _finish_timing(report: CiFixReport, *, started: float, started_at: str) -> None:
    total = time.monotonic() - started
    timing = dict(report.timing or {})
    test_duration = float(timing.get("test_duration_seconds", 0.0))
    agent_duration = float(timing.get("agent_duration_seconds", 0.0))
    timing.update({
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "total_duration_seconds": round(total, 3),
        "orchestration_duration_seconds": round(
            max(0.0, total - test_duration - agent_duration), 3
        ),
    })
    report.timing = timing


async def run_ci_fix_workflow(
    *,
    config: CiFixConfig,
    agent_factory: AgentFactory,
    cli_allowed_paths: tuple[str, ...] = (),
    targets: tuple[str, ...] = (),
    isolate: bool = True,
    keep_failed_worktree: bool = False,
    artifacts_dir: Path | None = None,
) -> CiWorkflowResult:
    """Run AutoCI-Fix and keep all repair attempts in one isolated worktree."""
    workflow_started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = generate_run_id()
    events = EventRecorder(run_id)
    events.emit("run_started", {
        "working_directory": str(config.cwd.resolve()),
        "isolated": isolate,
        "test_command": config.test_command,
    })
    original_cwd = config.cwd.resolve()
    if not isolate:
        report = await _execute(
            execution_cwd=original_cwd,
            config=config,
            agent_factory=agent_factory,
            cli_allowed_paths=cli_allowed_paths,
            targets=targets,
            events=events,
        )
        project_root = Path(report.workspace_policy["project_root"])
        paths = create_run_paths(
            project_root,
            run_id,
            artifacts_dir=artifacts_dir,
        )
        events.bind(paths.run_dir)
        report.run_id = run_id
        report.project_id = paths.project_id
        report.isolation = {"enabled": False}
        report.diff = {
            "available": False,
            "reason": "Diff capture is disabled for --no-isolate runs",
            "changed_file_count": 0,
            "insertions": 0,
            "deletions": 0,
        }
        _finish_timing(report, started=workflow_started, started_at=started_at)
        events.emit("run_finished", {
            "succeeded": report.succeeded,
            "timing": report.timing,
            "usage": report.usage,
            "cost": report.cost,
        })
        write_run_artifacts(
            paths.run_dir,
            report=report,
            metadata={
                "run_id": run_id,
                "project_id": paths.project_id,
                "project_root": str(project_root),
                "isolation": report.isolation,
            },
        )
        index_run(
            report,
            project_root=project_root,
            artifact_dir=paths.run_dir,
            database_path=paths.database_path,
        )
        return CiWorkflowResult(report=report, artifact_dir=paths.run_dir)

    session = WorktreeSession.create(
        original_cwd,
        artifacts_dir=artifacts_dir,
        run_id=run_id,
    )
    events.bind(session.artifact_dir)
    events.emit("worktree_created", {
        "base_commit": session.base_commit,
        "worktree_root": str(session.worktree_root),
    })
    report: CiFixReport | None = None
    snapshot = WorktreeSnapshot(status="", patch="")
    error: BaseException | None = None
    preserved = False
    try:
        report = await _execute(
            execution_cwd=session.execution_cwd,
            config=config,
            agent_factory=agent_factory,
            cli_allowed_paths=cli_allowed_paths,
            targets=targets,
            events=events,
        )
    except BaseException as exc:
        error = exc
    finally:
        try:
            snapshot = session.snapshot()
            events.emit("diff_captured", snapshot.diff_summary())
        except BaseException as snapshot_exc:
            if error is None:
                error = snapshot_exc

        succeeded = report.succeeded if report is not None else None
        if report is not None:
            report.run_id = run_id
            report.project_id = project_id(session.repo_root)
            report.diff = snapshot.diff_summary()
        preserved = keep_failed_worktree and succeeded is not True
        if not preserved:
            try:
                session.cleanup()
            except BaseException as cleanup_exc:
                if error is None:
                    error = cleanup_exc
                preserved = session.worktree_root.exists()

        isolation = session.isolation_metadata(succeeded=succeeded, preserved=preserved)
        if report is not None:
            report.isolation = isolation
            _finish_timing(report, started=workflow_started, started_at=started_at)
        events.emit("run_finished", {
            "succeeded": succeeded,
            "worktree_preserved": preserved,
            "timing": report.timing if report else None,
            "usage": report.usage if report else None,
            "cost": report.cost if report else None,
        })
        session.write_artifacts(
            report=report,
            isolation=isolation,
            snapshot=snapshot,
            error=str(error) if error is not None else None,
        )
        if report is not None:
            index_run(
                report,
                project_root=session.repo_root,
                artifact_dir=session.artifact_dir,
                database_path=get_database_path(artifacts_dir),
            )

    if error is not None:
        raise error
    assert report is not None
    return CiWorkflowResult(
        report=report,
        artifact_dir=session.artifact_dir,
        worktree_root=session.worktree_root if preserved else None,
        worktree_preserved=preserved,
    )
