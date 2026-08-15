"""Tests for the offline PyBugHive adapter."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.pybughive.catalog import PyBugHiveCase, load_catalog  # noqa: E402
from mini_claude.pybughive.runner import (  # noqa: E402
    PreparedCase,
    current_environment_test_command,
    prepare_case,
    run_case,
)


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


class OracleAgent:
    def __init__(self, policy):
        self.policy = policy

    async def chat(self, _prompt):
        (self.policy.project_root / "app.py").write_text(
            "def add(left, right):\n    return left + right\n", encoding="utf-8"
        )

    async def close(self):
        pass

    def get_usage_metrics(self):
        return {
            "model": "oracle",
            "provider": "test",
            "input_tokens": 100,
            "output_tokens": 20,
            "estimated_cost_usd": 0.001,
            "cost_is_estimate": True,
            "pricing_source": "test",
        }


class TestPyBugHiveCatalog(unittest.TestCase):
    def test_loads_case_and_filters_targets(self):
        payload = [{
            "username": "example",
            "repository": "demo",
            "installSteps": "pip install -e .",
            "issues": [{
                "id": 7,
                "title": "wrong result",
                "testSteps": "pipenv run pytest -- tests/test_app.py",
                "commits": [{
                    "hash": "fixed",
                    "parents": "buggy",
                    "stat": {
                        "files": [
                            {"filename": "src/app.py", "status": "modified"},
                            {"filename": "CHANGES.md", "status": "modified"},
                        ],
                        "tests": [{"filename": "tests/test_app.py", "status": "added"}],
                    },
                }],
            }],
        }]
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "dataset.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            case = load_catalog(source).get("demo-7")
        self.assertEqual(case.test_files, ("tests/test_app.py",))
        self.assertEqual(case.target_files, ("src/app.py",))
        self.assertEqual(case.install_steps, ("pip install -e .",))

    def test_converts_environment_runner(self):
        self.assertEqual(
            current_environment_test_command(("pipenv run pytest -- tests/test_app.py",)),
            "python -m pytest -- tests/test_app.py",
        )
        self.assertEqual(
            current_environment_test_command(("poetry run python -m pytest tests/test_app.py",)),
            "python -m pytest tests/test_app.py",
        )


class TestPyBugHivePreparation(unittest.TestCase):
    def test_prepares_buggy_parent_with_fixed_regression_test(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            upstream = temp_root / "upstream"
            upstream.mkdir()
            _git(upstream, "init", "-q")
            _git(upstream, "config", "user.email", "test@example.invalid")
            _git(upstream, "config", "user.name", "Test")
            (upstream / "app.py").write_text(
                "def add(left, right):\n    return left - right\n", encoding="utf-8"
            )
            _git(upstream, "add", ".")
            _git(upstream, "commit", "-q", "-m", "buggy")
            buggy_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=upstream,
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            (upstream / "app.py").write_text(
                "def add(left, right):\n    return left + right\n", encoding="utf-8"
            )
            tests = upstream / "tests"
            tests.mkdir()
            (tests / "test_app.py").write_text(
                "from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                encoding="utf-8",
            )
            _git(upstream, "add", ".")
            _git(upstream, "commit", "-q", "-m", "fix")
            fix_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=upstream,
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            payload = [{
                "username": "example",
                "repository": "demo",
                "issues": [{
                    "id": 7,
                    "title": "wrong result",
                    "testSteps": "pytest -q tests/test_app.py",
                    "commits": [{
                        "hash": fix_commit,
                        "parents": buggy_commit,
                        "stat": {
                            "files": [{"filename": "app.py", "status": "modified"}],
                            "tests": [{"filename": "tests/test_app.py", "status": "added"}],
                        },
                    }],
                }],
            }]
            source = temp_root / "dataset.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            case = load_catalog(source).get("demo-7")
            object.__setattr__(case, "clone_url", str(upstream))
            prepared = prepare_case(case, temp_root / "workspaces")

            self.assertIn("left - right", (prepared.project_root / "app.py").read_text())
            self.assertTrue((prepared.project_root / "tests" / "test_app.py").is_file())
            self.assertTrue((prepared.project_root / ".claude" / "settings.json").is_file())
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "tests/test_app.py"],
                cwd=prepared.project_root,
                capture_output=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            reused = prepare_case(case, temp_root / "workspaces")
            self.assertEqual(reused.project_root, prepared.project_root)


class TestPyBugHiveRunner(unittest.IsolatedAsyncioTestCase):
    async def test_scores_regression_repair_and_allowed_diff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            (root / "app.py").write_text(
                "def add(left, right):\n    return left - right\n", encoding="utf-8"
            )
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_app.py").write_text(
                "from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                encoding="utf-8",
            )
            (root / ".gitignore").write_text(
                "__pycache__/\n.pytest_cache/\n", encoding="utf-8"
            )
            settings = root / ".claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(json.dumps({
                "workspacePolicy": {
                    "readablePaths": ["."],
                    "writablePaths": ["app.py"],
                    "denyPaths": [".git/", ".env", ".env.*"],
                    "agentTools": ["read_file", "list_files", "grep_search", "edit_file", "write_file"],
                    "allowAgentShell": False,
                }
            }), encoding="utf-8")
            skill = root / ".claude" / "skills" / "pytest-repair" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\nname: pytest-repair\ndescription: Repair pytest failures\n"
                "when-to-use: When pytest fails\nuser-invocable: true\ncontext: inline\n"
                "allowed-tools: [read_file, edit_file]\n---\nRepair:\n$ARGUMENTS\n",
                encoding="utf-8",
            )
            _git(root, "init", "-q")
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Test")
            _git(root, "add", ".")
            _git(root, "commit", "-q", "-m", "fixture")
            case = PyBugHiveCase(
                id="demo-7", username="example", repository="demo", issue_id=7,
                title="wrong result", fix_commit="fixed", buggy_commit="buggy",
                test_files=("tests/test_app.py",), target_files=("app.py",),
                install_steps=(), test_steps=("pytest -q tests/test_app.py",),
                full_test_steps=(), clone_url="unused",
            )
            result = await run_case(
                PreparedCase(case, root, f'"{sys.executable}" -m pytest -q tests/test_app.py'),
                agent_factory=OracleAgent,
                artifacts_dir=Path(temp) / "artifacts",
            )
            self.assertTrue(result.passed, result.to_dict())
            self.assertTrue(result.initial_failed)
            self.assertEqual(result.changed_files, ["app.py"])
            self.assertEqual(result.input_tokens, 100)
            self.assertTrue(Path(result.artifact_dir, "pybughive-report.json").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
