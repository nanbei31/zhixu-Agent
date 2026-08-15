"""Tests for the native, no-Docker BugsInPy adapter."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.bugsinpy.catalog import load_catalog, parse_info_file  # noqa: E402
from mini_claude.bugsinpy.runner import (  # noqa: E402
    command_from_test_script,
    discover_production_roots,
    prepare_case,
    run_case,
)
from mini_claude.__main__ import parse_args  # noqa: E402
from mini_claude.workspace_policy import load_workspace_policy  # noqa: E402


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write_dataset(
    root: Path,
    *,
    clone_url: str,
    buggy_commit: str,
    fixed_commit: str,
) -> Path:
    bug = root / "projects" / "demo" / "bugs" / "1"
    bug.mkdir(parents=True)
    project = bug.parent.parent
    (project / "project.info").write_text(
        f'github_url="{clone_url}"\nstatus="OK"\ncause="N.A."\n',
        encoding="utf-8",
    )
    (bug / "bug.info").write_text(
        'python_version="3.9.0"\n'
        f'buggy_commit_id="{buggy_commit}"\n'
        f'fixed_commit_id="{fixed_commit}"\n'
        'test_file="tests/test_calc.py"\n'
        'pythonpath="demo_pkg"\n',
        encoding="utf-8",
    )
    (bug / "requirements.txt").write_text("pytest==6.2.5\n", encoding="utf-8")
    (bug / "run_test.sh").write_text(
        "python -m pytest -q tests/test_calc.py\n", encoding="utf-8",
    )
    (bug / "setup.sh").write_text("touch tests/__init__.py\n", encoding="utf-8")
    return root


def _write_upstream(root: Path) -> tuple[str, str]:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    package = root / "demo_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "calc.py").write_text(
        "def add(left, right):\n    return left - right\n", encoding="utf-8",
    )
    (package / "helper.py").write_text(
        "def identity(value):\n    return value\n", encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "buggy")
    buggy_commit = _git(root, "rev-parse", "HEAD")

    (package / "calc.py").write_text(
        "def add(left, right):\n    return left + right\n", encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(
        "from demo_pkg.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "fixed")
    return buggy_commit, _git(root, "rev-parse", "HEAD")


class OracleAgent:
    seen_targets = None
    seen_writable = None

    def __init__(self, policy):
        self.policy = policy
        type(self).seen_targets = policy.relative_targets()
        type(self).seen_writable = policy.relative_writable_roots()

    async def chat(self, _prompt):
        (self.policy.project_root / "demo_pkg" / "calc.py").write_text(
            "def add(left, right):\n    return left + right\n", encoding="utf-8",
        )

    async def close(self):
        pass

    def get_usage_metrics(self):
        return {
            "model": "oracle",
            "provider": "test",
            "input_tokens": 120,
            "output_tokens": 30,
            "estimated_cost_usd": 0.002,
            "cost_is_estimate": True,
            "pricing_source": "test",
        }


class TestBugsInPyCatalog(unittest.TestCase):
    def test_loads_official_metadata_without_sourcing_shell(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_dataset(
                root,
                clone_url="https://github.com/example/demo",
                buggy_commit="buggy",
                fixed_commit="fixed",
            )
            catalog = load_catalog(root)
            case = catalog.get("DEMO-1")

        self.assertEqual(len(catalog.cases), 1)
        self.assertEqual(case.python_version, "3.9.0")
        self.assertEqual(case.test_files, ("tests/test_calc.py",))
        self.assertEqual(case.python_paths, ("demo_pkg",))
        self.assertTrue(case.setup_script.name == "setup.sh")

    def test_rejects_non_assignment_metadata_instead_of_executing_it(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "bug.info"
            source.write_text('python_version="3.9"\nrm -rf /\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid BugsInPy metadata"):
                parse_info_file(source)

    def test_default_command_runs_the_copied_official_script(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "run_test.sh"
            script.write_text("python -m pytest -q tests/test_demo.py\n", encoding="utf-8")
            self.assertEqual(
                command_from_test_script(script),
                "bash .bugsinpy/run_test.sh",
            )

    def test_cli_accepts_list_and_end_to_end_defaults(self):
        with patch.object(sys, "argv", [
            "mini-claude-py",
            "--bugsinpy",
            "--bugsinpy-root",
            "BugsInPy",
            "--bugsinpy-list",
        ]):
            args = parse_args()
        self.assertTrue(args.bugsinpy)
        self.assertTrue(args.bugsinpy_list)
        self.assertEqual(args.bugsinpy_localization, "end-to-end")


class TestBugsInPyPreparation(unittest.TestCase):
    def test_prepares_end_to_end_and_oracle_without_target_leakage(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            upstream = temp_root / "upstream"
            buggy_commit, fixed_commit = _write_upstream(upstream)
            dataset = _write_dataset(
                temp_root / "BugsInPy",
                clone_url=str(upstream),
                buggy_commit=buggy_commit,
                fixed_commit=fixed_commit,
            )
            case = load_catalog(dataset).get("demo-1")
            workspaces = temp_root / "workspaces"
            command = f'"{sys.executable}" -m pytest -q tests/test_calc.py'

            end_to_end = prepare_case(
                case,
                workspaces,
                localization_mode="end-to-end",
                test_command=command,
            )
            oracle = prepare_case(
                case,
                workspaces,
                localization_mode="oracle",
                test_command=command,
            )

            self.assertIn(
                "left - right",
                (end_to_end.project_root / "demo_pkg" / "calc.py").read_text(),
            )
            self.assertTrue((end_to_end.project_root / "tests" / "test_calc.py").is_file())
            self.assertEqual(end_to_end.writable_paths, ("demo_pkg/",))
            self.assertEqual(oracle.writable_paths, ("demo_pkg/calc.py",))
            self.assertEqual(end_to_end.oracle_targets, ("demo_pkg/calc.py",))
            self.assertNotEqual(end_to_end.project_root, oracle.project_root)

            manifest_path = end_to_end.project_root / ".claude" / "bugsinpy-case.json"
            manifest_text = manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("oracle_targets", manifest_text)
            self.assertNotIn(fixed_commit, manifest_text)

            policy = load_workspace_policy(end_to_end.project_root)
            self.assertTrue(policy.check_path("demo_pkg/helper.py", write=True).allowed)
            self.assertFalse(policy.check_path("tests/test_calc.py", write=True).allowed)
            self.assertEqual(_git(end_to_end.project_root, "status", "--porcelain"), "")
            self.assertEqual(_git(oracle.project_root, "status", "--porcelain"), "")

            reused = prepare_case(
                case,
                workspaces,
                localization_mode="end-to-end",
            )
            self.assertEqual(reused.project_root, end_to_end.project_root)

    def test_discovers_package_root_without_gold_diff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_upstream(root / "repo")
            self.assertEqual(
                discover_production_roots(root / "repo", ("tests/test_calc.py",)),
                ("demo_pkg/",),
            )


class TestBugsInPyRunner(unittest.IsolatedAsyncioTestCase):
    async def test_scores_end_to_end_patch_in_an_independent_clone(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            upstream = temp_root / "upstream"
            buggy_commit, fixed_commit = _write_upstream(upstream)
            dataset = _write_dataset(
                temp_root / "BugsInPy",
                clone_url=str(upstream),
                buggy_commit=buggy_commit,
                fixed_commit=fixed_commit,
            )
            case = load_catalog(dataset).get("demo-1")
            command = f'"{sys.executable}" -m pytest -q tests/test_calc.py'
            prepared = prepare_case(
                case,
                temp_root / "workspaces",
                localization_mode="end-to-end",
                test_command=command,
                full_test_command=command,
            )

            result = await run_case(
                prepared,
                agent_factory=OracleAgent,
                artifacts_dir=temp_root / "artifacts",
            )

            self.assertTrue(result.passed, result.to_dict())
            self.assertTrue(result.initial_failed)
            self.assertTrue(result.patch_applied)
            self.assertTrue(result.regression_passed)
            self.assertTrue(result.full_tests_passed)
            self.assertTrue(result.allowed_diff)
            self.assertEqual(result.changed_files, ["demo_pkg/calc.py"])
            self.assertEqual(result.oracle_target_files, ["demo_pkg/calc.py"])
            self.assertEqual(result.input_tokens, 120)
            self.assertEqual(OracleAgent.seen_targets, ())
            self.assertEqual(OracleAgent.seen_writable, ("demo_pkg",))
            self.assertTrue(
                Path(result.artifact_dir, "bugsinpy-report.json").is_file()
            )

    async def test_oracle_mode_passes_gold_files_as_agent_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            upstream = temp_root / "upstream"
            buggy_commit, fixed_commit = _write_upstream(upstream)
            dataset = _write_dataset(
                temp_root / "BugsInPy",
                clone_url=str(upstream),
                buggy_commit=buggy_commit,
                fixed_commit=fixed_commit,
            )
            case = load_catalog(dataset).get("demo-1")
            command = f'"{sys.executable}" -m pytest -q tests/test_calc.py'
            prepared = prepare_case(
                case,
                temp_root / "workspaces",
                localization_mode="oracle",
                test_command=command,
            )

            result = await run_case(
                prepared,
                agent_factory=OracleAgent,
                artifacts_dir=temp_root / "artifacts",
            )

            self.assertTrue(result.passed, result.to_dict())
            self.assertEqual(OracleAgent.seen_targets, ("demo_pkg/calc.py",))
            self.assertEqual(OracleAgent.seen_writable, ("demo_pkg/calc.py",))


if __name__ == "__main__":
    unittest.main(verbosity=2)
