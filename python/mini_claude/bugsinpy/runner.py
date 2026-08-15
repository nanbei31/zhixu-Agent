"""Prepare and evaluate one BugsInPy repair through AutoCI-Fix."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Callable, Literal

from ..ci import CiFixConfig, run_ci_fix_workflow
from ..ci.runner import run_test_command
from ..workspace_policy import WorkspacePolicy
from .catalog import BugsInPyCase


LocalizationMode = Literal["end-to-end", "oracle"]
AgentFactory = Callable[[WorkspacePolicy], object]
SOURCE_SUFFIXES = {".py", ".pyi", ".pyx", ".pxd"}
EXCLUDED_TOP_LEVEL = {
    ".github", "benchmarks", "build", "doc", "docs", "example", "examples",
    "test", "tests", "testing", "tools",
}
EXCLUDED_FILENAMES = {
    "conftest.py", "setup.py", "setup.cfg", "pyproject.toml", "tox.ini",
}
DENY_PATHS = [
    ".git/", ".ssh/", ".venv/", "env/", ".env", ".env.*",
    "**/*.pem", "**/*.key", "test/", "tests/", "**/test/**",
    "**/tests/**", "test_*.py", "**/test_*.py", "*_test.py", "**/*_test.py",
]


@dataclass(frozen=True)
class PreparedCase:
    case: BugsInPyCase
    project_root: Path
    localization_mode: LocalizationMode
    test_command: str
    full_test_command: str | None
    writable_paths: tuple[str, ...]
    oracle_targets: tuple[str, ...]


@dataclass
class BugsInPyResult:
    case_id: str
    title: str
    project_root: str
    localization_mode: str
    test_command: str
    full_test_command: str | None
    initial_failed: bool
    autoci_passed: bool
    patch_applied: bool
    regression_passed: bool
    full_tests_passed: bool | None
    allowed_diff: bool
    passed: bool
    changed_files: list[str]
    oracle_target_files: list[str]
    attempts: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    duration_seconds: float
    artifact_dir: str | None
    error: str | None = None

    def to_dict(self) -> dict:
        return {"schema_version": 1, **asdict(self)}

    def render_text(self) -> str:
        full_state = "not run" if self.full_tests_passed is None else (
            "passed" if self.full_tests_passed else "failed"
        )
        return "\n".join([
            "BugsInPy report",
            f"  Case: {self.case_id} - {self.title}",
            f"  Localization: {self.localization_mode}",
            f"  Initial regression: {'confirmed' if self.initial_failed else 'not confirmed'}",
            f"  AutoCI repair: {'passed' if self.autoci_passed else 'failed'}",
            f"  Patch applies cleanly: {'yes' if self.patch_applied else 'no'}",
            f"  Independent regression test: {'passed' if self.regression_passed else 'failed'}",
            f"  Full regression tests: {full_state}",
            f"  Allowed diff: {'yes' if self.allowed_diff else 'no'}",
            f"  Attempts: {self.attempts}",
            f"  Changed files: {', '.join(self.changed_files) or '(none)'}",
            f"  Tokens: {self.input_tokens} input / {self.output_tokens} output",
            f"  Estimated cost: ${self.estimated_cost_usd:.6f}",
            f"  Duration: {self.duration_seconds:.2f}s",
            f"  Final score: {'PASS' if self.passed else 'FAIL'}",
        ])


def normalize_localization_mode(value: str) -> LocalizationMode:
    normalized = value.strip().lower()
    if normalized not in {"end-to-end", "oracle"}:
        raise ValueError("localization mode must be 'end-to-end' or 'oracle'")
    return normalized  # type: ignore[return-value]


def _git(cwd: Path | None, *args: str, binary: bool = False):
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
    )
    if completed.returncode != 0:
        stderr = completed.stderr
        stdout = completed.stdout
        if binary:
            stderr = stderr.decode("utf-8", errors="replace")
            stdout = stdout.decode("utf-8", errors="replace")
        raise RuntimeError(str(stderr).strip() or str(stdout).strip())
    return completed.stdout


def _normalized_path(value: str) -> str:
    return str(PurePosixPath(value.replace("\\", "/")))


def _is_test_path(path: str, explicit_tests: set[str]) -> bool:
    normalized = _normalized_path(path)
    lowered = normalized.lower()
    name = PurePosixPath(normalized).name.lower()
    parts = {part.lower() for part in PurePosixPath(normalized).parts}
    return (
        normalized in explicit_tests
        or "test" in parts
        or "tests" in parts
        or name == "conftest.py"
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _is_production_source(path: str, explicit_tests: set[str]) -> bool:
    normalized = _normalized_path(path)
    pure = PurePosixPath(normalized)
    if pure.suffix.lower() not in SOURCE_SUFFIXES:
        return False
    if _is_test_path(normalized, explicit_tests):
        return False
    if pure.name.lower() in EXCLUDED_FILENAMES:
        return False
    if pure.parts and pure.parts[0].lower() in EXCLUDED_TOP_LEVEL:
        return False
    return True


def _changed_files(project_root: Path, case: BugsInPyCase) -> tuple[str, ...]:
    output = _git(
        project_root,
        "diff", "--name-only", "--diff-filter=ACMRT",
        case.buggy_commit, case.fixed_commit, "--",
    )
    return tuple(
        dict.fromkeys(
            _normalized_path(line.strip())
            for line in str(output).splitlines()
            if line.strip()
        )
    )


def _tracked_source_files(project_root: Path, test_files: tuple[str, ...]) -> tuple[str, ...]:
    output = _git(project_root, "ls-files", "--", "*.py", "*.pyi", "*.pyx", "*.pxd")
    explicit_tests = {_normalized_path(path) for path in test_files}
    return tuple(
        _normalized_path(line.strip())
        for line in str(output).splitlines()
        if line.strip() and _is_production_source(line.strip(), explicit_tests)
    )


def discover_production_roots(
    project_root: Path,
    test_files: tuple[str, ...],
) -> tuple[str, ...]:
    """Find broad source roots without consulting the gold patch."""
    source_files = _tracked_source_files(project_root, test_files)
    package_tops = {
        PurePosixPath(path).parts[0]
        for path in source_files
        if len(PurePosixPath(path).parts) > 1
        and PurePosixPath(path).name == "__init__.py"
    }
    roots: list[str] = []
    for path in source_files:
        pure = PurePosixPath(path)
        if len(pure.parts) > 1 and pure.parts[0] == "src":
            candidate = "src/"
        elif len(pure.parts) > 1 and pure.parts[0] in package_tops:
            candidate = f"{pure.parts[0]}/"
        else:
            candidate = path
        if candidate not in roots:
            roots.append(candidate)
    return tuple(roots)


def command_from_test_script(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read BugsInPy test script {path}: {exc}") from exc
    commands = [
        line.strip().rstrip("\r")
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not commands:
        raise ValueError(f"BugsInPy test script is empty: {path}")
    return "bash .bugsinpy/run_test.sh"


def _write_git_blob(project_root: Path, revision: str, relative: str) -> None:
    content = _git(project_root, "show", f"{revision}:{relative}", binary=True)
    destination = project_root / Path(relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


def _copy_metadata(case: BugsInPyCase, project_root: Path) -> None:
    destination = project_root / ".bugsinpy"
    destination.mkdir(parents=True, exist_ok=True)
    for source, name in (
        (case.requirements_file, "requirements.txt"),
        (case.test_script, "run_test.sh"),
        (case.setup_script, "setup.sh"),
    ):
        if source is not None and source.is_file():
            shutil.copyfile(source, destination / name)


def _write_agent_config(project_root: Path, writable_paths: tuple[str, ...]) -> None:
    settings = {
        "workspacePolicy": {
            "readablePaths": ["."],
            "writablePaths": list(writable_paths),
            "denyPaths": DENY_PATHS,
            "agentTools": [
                "read_file", "list_files", "grep_search", "edit_file", "write_file",
            ],
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


def _append_gitignore(project_root: Path) -> None:
    ignore_path = project_root / ".gitignore"
    existing = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
    additions = """
# Mini Claude BugsInPy benchmark
.claude-worktrees/
.pytest_cache/
__pycache__/
*.egg-info/
build/
dist/
env/
"""
    ignore_path.write_text(existing.rstrip() + "\n" + additions.lstrip(), encoding="utf-8")


def prepare_case(
    case: BugsInPyCase,
    workspaces_root: Path,
    *,
    localization_mode: str = "end-to-end",
    test_command: str | None = None,
    full_test_command: str | None = None,
) -> PreparedCase:
    mode = normalize_localization_mode(localization_mode)
    workspace_name = f"{case.id}-{mode}"
    project_root = workspaces_root.expanduser().resolve() / workspace_name
    resolved_command = test_command or command_from_test_script(case.test_script)

    if project_root.exists():
        manifest_path = project_root / ".claude" / "bugsinpy-case.json"
        if not manifest_path.is_file():
            raise FileExistsError(
                f"BugsInPy workspace exists without a case manifest: {project_root}"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("case_id") != case.id or manifest.get("localization_mode") != mode:
            raise ValueError(f"BugsInPy workspace manifest does not match {case.id}/{mode}")
        writable = tuple(str(value) for value in manifest.get("writable_paths") or [])
        explicit_tests = {_normalized_path(item) for item in case.test_files}
        oracle_targets = tuple(
            path for path in _changed_files(project_root, case)
            if _is_production_source(path, explicit_tests)
        )
        if not writable or not oracle_targets:
            raise ValueError(f"BugsInPy workspace manifest is incomplete: {manifest_path}")
        return PreparedCase(
            case=case,
            project_root=project_root,
            localization_mode=mode,
            test_command=test_command or str(manifest.get("test_command") or ""),
            full_test_command=(
                full_test_command
                if full_test_command is not None
                else manifest.get("full_test_command")
            ),
            writable_paths=writable,
            oracle_targets=oracle_targets,
        )

    project_root.parent.mkdir(parents=True, exist_ok=True)
    _git(None, "clone", "--quiet", "--no-checkout", case.clone_url, str(project_root))
    try:
        _git(project_root, "checkout", "--quiet", "--detach", case.buggy_commit)
        oracle_targets = tuple(
            path for path in _changed_files(project_root, case)
            if _is_production_source(path, {_normalized_path(item) for item in case.test_files})
        )
        if not oracle_targets:
            raise ValueError(f"BugsInPy case {case.id} has no production-code gold targets")
        for relative in case.test_files:
            _write_git_blob(project_root, case.fixed_commit, relative)

        writable_paths = (
            oracle_targets
            if mode == "oracle"
            else discover_production_roots(project_root, case.test_files)
        )
        if not writable_paths:
            raise ValueError(f"BugsInPy case {case.id} has no safe writable production paths")
        _copy_metadata(case, project_root)
        _write_agent_config(project_root, writable_paths)
        _append_gitignore(project_root)
        manifest = {
            "schema_version": 1,
            "case_id": case.id,
            "project": case.project,
            "bug_id": case.bug_id,
            "python_version": case.python_version,
            "localization_mode": mode,
            "test_files": list(case.test_files),
            "test_command": resolved_command,
            "full_test_command": full_test_command,
            "writable_paths": list(writable_paths),
        }
        manifest_path = project_root / ".claude" / "bugsinpy-case.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        _git(project_root, "config", "user.email", "bugsinpy@example.invalid")
        _git(project_root, "config", "user.name", "Mini Claude BugsInPy")
        _git(project_root, "add", ".")
        _git(project_root, "commit", "--quiet", "-m", f"BugsInPy fixture {case.id} ({mode})")
    except BaseException:
        # Keep a failed checkout for diagnosis; preparation never deletes user data.
        raise

    return PreparedCase(
        case=case,
        project_root=project_root,
        localization_mode=mode,
        test_command=resolved_command,
        full_test_command=full_test_command,
        writable_paths=writable_paths,
        oracle_targets=oracle_targets,
    )


def _path_is_allowed(path: str, roots: tuple[str, ...]) -> bool:
    normalized = _normalized_path(path)
    for root in roots:
        normalized_root = _normalized_path(root).rstrip("/")
        if normalized == normalized_root or normalized.startswith(normalized_root + "/"):
            return True
    return False


def _independent_evaluation(
    prepared: PreparedCase,
    patch: Path,
    *,
    timeout_seconds: float,
) -> tuple[bool, bool, bool | None, str | None]:
    with tempfile.TemporaryDirectory(prefix=f"bugsinpy-eval-{prepared.case.id}-") as temp:
        evaluation_root = Path(temp) / "project"
        try:
            _git(None, "clone", "--quiet", "--no-hardlinks", str(prepared.project_root), str(evaluation_root))
            _git(evaluation_root, "apply", "--check", str(patch))
            _git(evaluation_root, "apply", str(patch))
        except Exception as exc:
            return False, False, None, str(exc)

        regression = run_test_command(
            prepared.test_command,
            evaluation_root,
            timeout_seconds,
        )
        if not regression.succeeded:
            return True, False, None, regression.combined_output[-2000:]
        if prepared.full_test_command is None:
            return True, True, None, None
        full = run_test_command(
            prepared.full_test_command,
            evaluation_root,
            timeout_seconds,
        )
        return (
            True,
            True,
            full.succeeded,
            None if full.succeeded else full.combined_output[-2000:],
        )


async def run_case(
    prepared: PreparedCase,
    *,
    agent_factory: AgentFactory,
    max_attempts: int = 2,
    timeout_seconds: float = 300.0,
    artifacts_dir: Path | None = None,
) -> BugsInPyResult:
    config = CiFixConfig(
        test_command=prepared.test_command,
        cwd=prepared.project_root,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        repair_skill_name="pytest-repair",
    )
    targets = prepared.oracle_targets if prepared.localization_mode == "oracle" else ()
    workflow = await run_ci_fix_workflow(
        config=config,
        agent_factory=agent_factory,
        targets=targets,
        artifacts_dir=artifacts_dir,
    )
    report = workflow.report
    changed_files = [item["path"] for item in (report.diff or {}).get("files", [])]
    allowed_diff = bool(changed_files) and all(
        _path_is_allowed(path, prepared.writable_paths) for path in changed_files
    )
    patch_applied = False
    regression_passed = False
    full_tests_passed: bool | None = None
    evaluation_error: str | None = None
    if workflow.artifact_dir:
        patch_applied, regression_passed, full_tests_passed, evaluation_error = (
            _independent_evaluation(
                prepared,
                workflow.artifact_dir / "changes.patch",
                timeout_seconds=timeout_seconds,
            )
        )
    initial_failed = not report.initial.succeeded
    autoci_passed = report.succeeded
    usage = report.usage or {}
    result = BugsInPyResult(
        case_id=prepared.case.id,
        title=prepared.case.title,
        project_root=str(prepared.project_root),
        localization_mode=prepared.localization_mode,
        test_command=prepared.test_command,
        full_test_command=prepared.full_test_command,
        initial_failed=initial_failed,
        autoci_passed=autoci_passed,
        patch_applied=patch_applied,
        regression_passed=regression_passed,
        full_tests_passed=full_tests_passed,
        allowed_diff=allowed_diff,
        passed=all([
            initial_failed,
            autoci_passed,
            patch_applied,
            regression_passed,
            allowed_diff,
            full_tests_passed is not False,
        ]),
        changed_files=changed_files,
        oracle_target_files=list(prepared.oracle_targets),
        attempts=len(report.attempts),
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        estimated_cost_usd=float((report.cost or {}).get("estimated_usd", 0.0)),
        duration_seconds=float((report.timing or {}).get("total_duration_seconds", 0.0)),
        artifact_dir=str(workflow.artifact_dir) if workflow.artifact_dir else None,
        error=evaluation_error,
    )
    if workflow.artifact_dir:
        report_path = workflow.artifact_dir / "bugsinpy-report.json"
        report_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result
