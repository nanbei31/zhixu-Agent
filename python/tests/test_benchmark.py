"""Tests for the declarative pytest repair benchmark."""

import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.benchmark import load_suite, run_benchmark  # noqa: E402
from mini_claude.benchmark.catalog import materialize_case  # noqa: E402
from mini_claude.benchmark.runner import _evaluate  # noqa: E402


class OracleAgent:
    def __init__(self, policy, solution):
        self.policy = policy
        self.solution = solution
        self.calls = 0

    async def chat(self, user_message):
        self.calls += 1
        for relative, content in self.solution.items():
            (self.policy.project_root / relative).write_text(content, encoding="utf-8")

    async def close(self):
        pass

    def get_usage_metrics(self):
        return {
            "model": "oracle",
            "provider": "test",
            "input_tokens": self.calls * 100,
            "output_tokens": self.calls * 20,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "total_accounted_tokens": self.calls * 120,
            "turns": self.calls,
            "estimated_cost_usd": self.calls * 0.001,
            "cost_is_estimate": True,
            "pricing_source": "test",
        }


class TestBenchmarkCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = load_suite()

    def test_default_suite_has_forty_balanced_cases(self):
        self.assertEqual(len(self.suite.cases), 40)
        categories = Counter(case.category for case in self.suite.cases)
        self.assertEqual(len(categories), 8)
        self.assertEqual(set(categories.values()), {5})
        self.assertEqual(len({case.id for case in self.suite.cases}), 40)

    def test_each_case_starts_failing_and_oracle_passes(self):
        for case in self.suite.cases:
            with self.subTest(case=case.id), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                materialize_case(case, root)
                initial = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "tests/test_public.py"],
                    cwd=root,
                    capture_output=True,
                    timeout=30,
                )
                self.assertNotEqual(initial.returncode, 0)

    def test_hidden_evaluator_accepts_oracle_patch(self):
        case = self.suite.cases[0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            materialize_case(case, root)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            for relative, content in case.solution.items():
                (root / relative).write_text(content, encoding="utf-8")
            patch = root / "oracle.patch"
            patch.write_text(
                subprocess.run(
                    ["git", "diff", "--binary"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout,
                encoding="utf-8",
            )
            applied, hidden_passed, error = _evaluate(case, patch)
            self.assertTrue(applied)
            self.assertTrue(hidden_passed, error)


class TestBenchmarkRunner(unittest.IsolatedAsyncioTestCase):
    async def test_oracle_agent_produces_scored_json_and_csv(self):
        suite = load_suite()
        case = suite.cases[0]
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "benchmark-output"
            report, run_dir = await run_benchmark(
                suite,
                agent_factory=lambda policy: OracleAgent(policy, case.solution),
                case_ids=(case.id,),
                output_dir=output,
            )

            self.assertEqual(run_dir, output.resolve())
            self.assertEqual(len(report.scores), 1)
            score = report.scores[0]
            self.assertTrue(score.passed, score.error)
            self.assertTrue(score.hidden_tests_passed)
            self.assertTrue(score.allowed_diff)
            self.assertEqual(score.changed_files, ["app.py"])
            self.assertEqual(report.summary()["success_rate"], 1.0)
            self.assertEqual(report.summary()["success_at_1"], 1.0)
            self.assertEqual(report.summary()["input_tokens"], 100)
            data = json.loads(
                (run_dir / "benchmark-report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(data["summary"]["passed_runs"], 1)
            self.assertTrue((run_dir / "benchmark-results.csv").is_file())
            self.assertTrue((run_dir / "autoci-runs" / "autoci.db").is_file())

    async def test_rejects_unknown_case(self):
        with self.assertRaisesRegex(ValueError, "unknown benchmark case"):
            await run_benchmark(
                load_suite(),
                agent_factory=lambda policy: None,
                case_ids=("does-not-exist",),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
