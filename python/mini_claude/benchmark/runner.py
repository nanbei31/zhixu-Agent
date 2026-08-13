"""Execute AutoCI-Fix against benchmark cases and score hidden tests."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Callable

from ..ci import CiFixConfig, run_ci_fix_workflow
from ..ci.storage import generate_run_id, get_data_root
from ..workspace_policy import WorkspacePolicy
from .catalog import BenchmarkCase, BenchmarkSuite, materialize_case


@dataclass
class CaseScore:
    case_id: str
    title: str
    category: str
    difficulty: str
    repetition: int
    passed: bool = False
    autoci_passed: bool = False
    hidden_tests_passed: bool = False
    patch_applied: bool = False
    allowed_diff: bool = False
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    duration_seconds: float = 0.0
    changed_files: list[str] = field(default_factory=list)
    artifact_dir: str | None = None
    error: str | None = None


@dataclass
class BenchmarkReport:
    run_id: str
    suite: str
    suite_version: int
    started_at: str
    finished_at: str
    scores: list[CaseScore]

    def summary(self) -> dict:
        total = len(self.scores)
        passed = sum(score.passed for score in self.scores)
        first_attempt = sum(score.passed and score.attempts <= 1 for score in self.scores)
        return {
            "total_runs": total,
            "passed_runs": passed,
            "success_rate": passed / total if total else 0.0,
            "success_at_1": first_attempt / total if total else 0.0,
            "hidden_test_pass_rate": (
                sum(score.hidden_tests_passed for score in self.scores) / total if total else 0.0
            ),
            "policy_compliance_rate": (
                sum(score.allowed_diff for score in self.scores) / total if total else 0.0
            ),
            "input_tokens": sum(score.input_tokens for score in self.scores),
            "output_tokens": sum(score.output_tokens for score in self.scores),
            "estimated_cost_usd": round(
                sum(score.estimated_cost_usd for score in self.scores), 6
            ),
            "average_duration_seconds": (
                sum(score.duration_seconds for score in self.scores) / total if total else 0.0
            ),
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "suite": self.suite,
            "suite_version": self.suite_version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary(),
            "scores": [asdict(score) for score in self.scores],
        }

    def render_text(self) -> str:
        summary = self.summary()
        return "\n".join([
            "Benchmark report",
            f"  Suite: {self.suite} v{self.suite_version}",
            f"  Runs: {summary['passed_runs']}/{summary['total_runs']} passed",
            f"  Success@1: {summary['success_at_1']:.1%}",
            f"  Final success: {summary['success_rate']:.1%}",
            f"  Hidden tests: {summary['hidden_test_pass_rate']:.1%}",
            f"  Policy compliance: {summary['policy_compliance_rate']:.1%}",
            f"  Tokens: {summary['input_tokens']} input / {summary['output_tokens']} output",
            f"  Estimated cost: ${summary['estimated_cost_usd']:.6f}",
            f"  Average duration: {summary['average_duration_seconds']:.2f}s",
        ])


AgentFactory = Callable[[WorkspacePolicy], object]


def _git(root: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8"
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())


def _prepare_project(case: BenchmarkCase, root: Path) -> None:
    materialize_case(case, root)
    settings = {
        "workspacePolicy": {
            "readablePaths": ["."],
            "writablePaths": list(case.allowed_changes),
            "denyPaths": [".git/", ".env", ".env.*", "**/*.pem", "**/*.key"],
            "agentTools": ["read_file", "list_files", "grep_search", "edit_file", "write_file"],
            "allowAgentShell": False,
        }
    }
    settings_path = root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    skill_source = files("mini_claude.benchmark").joinpath("assets/pytest-repair/SKILL.md")
    skill_path = root / ".claude" / "skills" / "pytest-repair" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(skill_source.read_text(encoding="utf-8"), encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "benchmark@example.invalid")
    _git(root, "config", "user.name", "Mini Claude Benchmark")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "benchmark fixture")


def _evaluate(case: BenchmarkCase, patch: Path) -> tuple[bool, bool, str | None]:
    with tempfile.TemporaryDirectory(prefix=f"benchmark-eval-{case.id}-") as temp:
        root = Path(temp)
        materialize_case(case, root)
        apply_result = subprocess.run(
            ["git", "apply", str(patch)],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if apply_result.returncode != 0:
            return False, False, apply_result.stderr.strip() or "patch could not be applied"
        hidden = root / "tests" / "test_hidden.py"
        hidden.parent.mkdir(parents=True, exist_ok=True)
        hidden.write_text(case.hidden_test, encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/test_public.py", "tests/test_hidden.py"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        error = None if completed.returncode == 0 else (completed.stdout + completed.stderr)[-2000:]
        return True, completed.returncode == 0, error


def _write_report(report: BenchmarkReport, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "benchmark-report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    fieldnames = list(asdict(report.scores[0]).keys()) if report.scores else []
    with (output_dir / "benchmark-results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            for score in report.scores:
                row = asdict(score)
                row["changed_files"] = ";".join(row["changed_files"])
                writer.writerow(row)


async def run_benchmark(
    suite: BenchmarkSuite,
    *,
    agent_factory: AgentFactory,
    case_ids: tuple[str, ...] = (),
    categories: tuple[str, ...] = (),
    limit: int | None = None,
    repetitions: int = 1,
    max_attempts: int = 2,
    timeout_seconds: float = 300.0,
    output_dir: Path | None = None,
) -> tuple[BenchmarkReport, Path]:
    unknown = set(case_ids) - {case.id for case in suite.cases}
    if unknown:
        raise ValueError(f"unknown benchmark case IDs: {sorted(unknown)}")
    selected = [
        case for case in suite.cases
        if (not case_ids or case.id in case_ids)
        and (not categories or case.category in categories)
    ]
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise ValueError("benchmark selection is empty")
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")

    run_id = generate_run_id()
    run_root = (output_dir or (get_data_root() / "benchmarks" / run_id)).resolve()
    started_at = datetime.now(timezone.utc).isoformat()
    scores: list[CaseScore] = []
    artifacts = run_root / "autoci-runs"
    for repetition in range(1, repetitions + 1):
        for case in selected:
            score = CaseScore(
                case_id=case.id,
                title=case.title,
                category=case.category,
                difficulty=case.difficulty,
                repetition=repetition,
            )
            started = time.monotonic()
            try:
                with tempfile.TemporaryDirectory(prefix=f"benchmark-run-{case.id}-") as temp:
                    project = Path(temp)
                    _prepare_project(case, project)
                    config = CiFixConfig(
                        test_command=f'"{sys.executable}" -B -m pytest -q tests/test_public.py',
                        cwd=project,
                        max_attempts=max_attempts,
                        timeout_seconds=timeout_seconds,
                        repair_skill_name="pytest-repair",
                    )
                    result = await run_ci_fix_workflow(
                        config=config,
                        agent_factory=agent_factory,
                        targets=case.allowed_changes,
                        artifacts_dir=artifacts,
                    )
                    report = result.report
                    score.autoci_passed = report.succeeded
                    score.attempts = len(report.attempts)
                    score.artifact_dir = str(result.artifact_dir)
                    usage = report.usage or {}
                    score.input_tokens = int(usage.get("input_tokens", 0))
                    score.output_tokens = int(usage.get("output_tokens", 0))
                    score.estimated_cost_usd = float((report.cost or {}).get("estimated_usd", 0.0))
                    score.changed_files = [item["path"] for item in (report.diff or {}).get("files", [])]
                    score.allowed_diff = bool(score.changed_files) and set(score.changed_files) <= set(case.allowed_changes)
                    score.patch_applied, score.hidden_tests_passed, evaluation_error = _evaluate(
                        case, result.artifact_dir / "changes.patch"
                    )
                    score.error = evaluation_error
                    score.passed = all([
                        score.autoci_passed,
                        score.patch_applied,
                        score.hidden_tests_passed,
                        score.allowed_diff,
                    ])
            except Exception as exc:
                score.error = str(exc)
            score.duration_seconds = round(time.monotonic() - started, 3)
            scores.append(score)
            state = "PASS" if score.passed else "FAIL"
            print(f"[Benchmark] {case.id} repetition {repetition}: {state}", flush=True)

    report = BenchmarkReport(
        run_id=run_id,
        suite=suite.name,
        suite_version=suite.version,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
        scores=scores,
    )
    _write_report(report, run_root)
    return report, run_root
