"""Prepare and score one PyBugHive case through the existing AutoCI workflow."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path
from typing import Callable

from ..ci import CiFixConfig, run_ci_fix_workflow
from ..workspace_policy import WorkspacePolicy
from .catalog import PyBugHiveCase


AgentFactory = Callable[[WorkspacePolicy], object]


@dataclass(frozen=True)
class PreparedCase:
    case: PyBugHiveCase
    project_root: Path
    test_command: str


@dataclass
class PyBugHiveResult:
    case_id: str
    title: str
    project_root: str
    test_command: str
    initial_failed: bool
    autoci_passed: bool
    allowed_diff: bool
    passed: bool
    changed_files: list[str]
    attempts: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    artifact_dir: str | None
    error: str | None = None

    def to_dict(self) -> dict:
        return {"schema_version": 1, **asdict(self)}

    def render_text(self) -> str:
        return "\n".join([
            "PyBugHive report",
            f"  Case: {self.case_id} - {self.title}",
            f"  Initial regression: {'confirmed' if self.initial_failed else 'not confirmed'}",
            f"  AutoCI repair: {'passed' if self.autoci_passed else 'failed'}",
            f"  Allowed diff: {'yes' if self.allowed_diff else 'no'}",
            f"  Attempts: {self.attempts}",
            f"  Changed files: {', '.join(self.changed_files) or '(none)'}",
            f"  Tokens: {self.input_tokens} input / {self.output_tokens} output",
            f"  Estimated cost: ${self.estimated_cost_usd:.6f}",
            f"  Final score: {'PASS' if self.passed else 'FAIL'}",
        ])


def _git(cwd: Path | None, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout


def current_environment_test_command(steps: tuple[str, ...]) -> str:
    converted: list[str] = []
    for step in steps:
        command = re.sub(r"^(?:pipenv|poetry)\s+run\s+", "", step.strip())
        command = re.sub(r"^pytest\b", "python -m pytest", command)
        converted.append(command)
    return " && ".join(converted)


def _write_git_blob(project_root: Path, revision: str, relative: str) -> None:
    completed = subprocess.run(
        ["git", "show", f"{revision}:{relative}"],
        cwd=project_root,
        capture_output=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"cannot restore regression test {relative}: {message}")
    destination = project_root / Path(relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(completed.stdout)


def _write_agent_config(project_root: Path, case: PyBugHiveCase) -> None:
    settings = {
        "workspacePolicy": {
            "readablePaths": ["."],
            "writablePaths": list(case.target_files),
            "denyPaths": [
                ".git/", ".env", ".env.*", ".venv/", "**/*.pem", "**/*.key",
            ],
            "agentTools": ["read_file", "list_files", "grep_search", "edit_file", "write_file"],
            "allowAgentShell": False,
        }
    }
    settings_path = project_root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    skill_source = files("mini_claude.benchmark").joinpath("assets/pytest-repair/SKILL.md")
    skill_path = project_root / ".claude" / "skills" / "pytest-repair" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(skill_source.read_text(encoding="utf-8"), encoding="utf-8")


def prepare_case(
    case: PyBugHiveCase,
    workspaces_root: Path,
    *,
    test_command: str | None = None,
) -> PreparedCase:
    if not case.test_files:
        raise ValueError(f"PyBugHive case {case.id} has no regression test files")
    if not case.target_files:
        raise ValueError(f"PyBugHive case {case.id} has no safe production-code targets")
    project_root = workspaces_root.expanduser().resolve() / case.id
    if project_root.exists():
        manifest_path = project_root / ".claude" / "pybughive-case.json"
        if not manifest_path.is_file():
            raise FileExistsError(
                f"PyBugHive workspace already exists but has no case manifest: {project_root}"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("case_id") != case.id:
            raise ValueError(f"PyBugHive workspace manifest does not match {case.id}")
        resolved_command = test_command or str(manifest.get("test_command") or "")
        if not resolved_command:
            raise ValueError(f"PyBugHive case {case.id} has no test command")
        return PreparedCase(case=case, project_root=project_root, test_command=resolved_command)
    project_root.parent.mkdir(parents=True, exist_ok=True)
    _git(None, "clone", "--quiet", "--no-checkout", case.clone_url, str(project_root))
    try:
        _git(project_root, "checkout", "--quiet", case.buggy_commit)
        for relative in case.test_files:
            _write_git_blob(project_root, case.fix_commit, relative)
        _write_agent_config(project_root, case)
        ignore_path = project_root / ".gitignore"
        existing = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
        additions = "\n# Mini Claude benchmark\n.claude-worktrees/\n.pytest_cache/\n__pycache__/\n"
        ignore_path.write_text(existing.rstrip() + additions, encoding="utf-8")
        _git(project_root, "config", "user.email", "pybughive@example.invalid")
        _git(project_root, "config", "user.name", "Mini Claude PyBugHive")
        _git(project_root, "add", ".")
        _git(project_root, "commit", "--quiet", "-m", f"PyBugHive fixture {case.id}")
    except BaseException:
        # Keep a failed checkout for diagnosis; preparation never deletes user data.
        raise

    resolved_command = test_command or current_environment_test_command(case.test_steps)
    if not resolved_command:
        raise ValueError(f"PyBugHive case {case.id} has no test command")
    manifest = {
        "case_id": case.id,
        "title": case.title,
        "buggy_commit": case.buggy_commit,
        "fix_commit": case.fix_commit,
        "test_files": list(case.test_files),
        "target_files": list(case.target_files),
        "test_command": resolved_command,
        "install_steps": list(case.install_steps),
    }
    manifest_path = project_root / ".claude" / "pybughive-case.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    _git(project_root, "add", str(manifest_path.relative_to(project_root)))
    _git(project_root, "commit", "--quiet", "--amend", "--no-edit")
    return PreparedCase(case=case, project_root=project_root, test_command=resolved_command)


async def run_case(
    prepared: PreparedCase,
    *,
    agent_factory: AgentFactory,
    max_attempts: int = 2,
    timeout_seconds: float = 300.0,
    artifacts_dir: Path | None = None,
) -> PyBugHiveResult:
    config = CiFixConfig(
        test_command=prepared.test_command,
        cwd=prepared.project_root,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        repair_skill_name="pytest-repair",
    )
    workflow = await run_ci_fix_workflow(
        config=config,
        agent_factory=agent_factory,
        targets=prepared.case.target_files,
        artifacts_dir=artifacts_dir,
    )
    report = workflow.report
    changed_files = [item["path"] for item in (report.diff or {}).get("files", [])]
    allowed_diff = bool(changed_files) and set(changed_files) <= set(prepared.case.target_files)
    initial_failed = not report.initial.succeeded
    autoci_passed = report.succeeded
    usage = report.usage or {}
    result = PyBugHiveResult(
        case_id=prepared.case.id,
        title=prepared.case.title,
        project_root=str(prepared.project_root),
        test_command=prepared.test_command,
        initial_failed=initial_failed,
        autoci_passed=autoci_passed,
        allowed_diff=allowed_diff,
        passed=initial_failed and autoci_passed and allowed_diff,
        changed_files=changed_files,
        attempts=len(report.attempts),
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        estimated_cost_usd=float((report.cost or {}).get("estimated_usd", 0.0)),
        artifact_dir=str(workflow.artifact_dir) if workflow.artifact_dir else None,
    )
    if workflow.artifact_dir:
        report_path = workflow.artifact_dir / "pybughive-report.json"
        report_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result
