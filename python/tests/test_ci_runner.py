"""Tests for the AutoCI-Fix orchestration without a live model."""

import sys
import os
import tempfile
import unittest
from pathlib import Path

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.ci import (  # noqa: E402
    CiFixConfig,
    CommandResult,
    build_repair_prompt,
    parse_pytest_output,
    run_ci_fix,
)
from mini_claude.skills import get_skill_by_name  # noqa: E402


def _result(exit_code, output):
    return CommandResult(
        command="pytest -q",
        cwd=str(Path.cwd()),
        exit_code=exit_code,
        stdout=output,
        stderr="",
        duration_seconds=0.1,
    )


FAILED_OUTPUT = """
calculator_test.py:6: AssertionError
FAILED calculator_test.py::test_add - assert -1 == 5
1 failed in 0.10s
"""


class FakeAgent:
    def __init__(self):
        self.prompts = []

    async def chat(self, user_message):
        self.prompts.append(user_message)

    def get_usage_metrics(self):
        attempts = len(self.prompts)
        return {
            "model": "fake-model",
            "provider": "fake-provider",
            "input_tokens": attempts * 100,
            "output_tokens": attempts * 20,
            "cache_read_tokens": attempts * 5,
            "cache_creation_tokens": 0,
            "total_accounted_tokens": attempts * 125,
            "turns": attempts,
            "estimated_cost_usd": attempts * 0.002,
            "cost_is_estimate": True,
            "pricing_source": "test-pricing",
        }


class QueueRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, command, cwd, timeout_seconds):
        self.calls.append((command, cwd, timeout_seconds))
        return self.results.pop(0)


class TestCiFixRunner(unittest.IsolatedAsyncioTestCase):
    async def test_skips_agent_when_tests_already_pass(self):
        agent = FakeAgent()
        command_runner = QueueRunner([_result(0, "3 passed in 0.05s")])
        config = CiFixConfig(test_command="pytest -q", cwd=Path("."))

        report = await run_ci_fix(
            agent,
            config,
            command_runner,
            repair_skill=get_skill_by_name("pytest-repair"),
        )

        self.assertTrue(report.succeeded)
        self.assertEqual(agent.prompts, [])
        self.assertEqual(len(command_runner.calls), 1)

    async def test_repairs_then_revalidates(self):
        agent = FakeAgent()
        command_runner = QueueRunner([
            _result(1, FAILED_OUTPUT),
            _result(0, "1 passed in 0.06s"),
        ])
        config = CiFixConfig(
            test_command="pytest -q",
            cwd=Path("."),
            max_attempts=2,
            targets=("src/calculator.py",),
            writable_paths=("src",),
            workspace_policy={"project_root": "/project"},
        )

        report = await run_ci_fix(
            agent,
            config,
            command_runner,
            repair_skill=get_skill_by_name("pytest-repair"),
        )

        self.assertTrue(report.succeeded)
        self.assertEqual(len(report.attempts), 1)
        self.assertEqual(len(agent.prompts), 1)
        self.assertIn("calculator_test.py::test_add", agent.prompts[0])
        self.assertIn("src/calculator.py", agent.prompts[0])
        self.assertIn("运行时强制可写路径\n\n- src", agent.prompts[0])
        self.assertIn("Do not commit", agent.prompts[0])
        self.assertEqual(len(command_runner.calls), 2)
        self.assertEqual(report.to_dict()["workspace_policy"]["project_root"], "/project")
        self.assertEqual(report.skill_name, "pytest-repair")
        self.assertTrue(report.skill_loaded)
        self.assertEqual(report.attempts[0].context_summary["classification"], "assertion")
        self.assertEqual(report.attempts[0].usage["input_tokens"], 100)
        self.assertEqual(report.attempts[0].usage["output_tokens"], 20)
        self.assertAlmostEqual(report.attempts[0].cost["estimated_usd"], 0.002)
        self.assertGreaterEqual(report.attempts[0].agent_duration_seconds, 0.0)

    async def test_stops_after_attempt_limit(self):
        agent = FakeAgent()
        command_runner = QueueRunner([
            _result(1, FAILED_OUTPUT),
            _result(1, FAILED_OUTPUT),
            _result(1, FAILED_OUTPUT),
        ])
        config = CiFixConfig(
            test_command="pytest -q",
            cwd=Path("."),
            max_attempts=2,
        )

        report = await run_ci_fix(
            agent,
            config,
            command_runner,
            repair_skill=get_skill_by_name("pytest-repair"),
        )

        self.assertFalse(report.succeeded)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(len(report.attempts), 2)
        self.assertEqual(len(agent.prompts), 2)
        self.assertEqual(
            report.attempts[1].context_summary["previous_attempts"][0]["number"],
            1,
        )

    async def test_records_missing_skill_and_uses_fallback_prompt(self):
        agent = FakeAgent()
        command_runner = QueueRunner([
            _result(1, FAILED_OUTPUT),
            _result(0, "1 passed in 0.06s"),
        ])
        config = CiFixConfig(
            test_command="pytest -q",
            repair_skill_name="missing-repair-skill",
        )

        report = await run_ci_fix(agent, config, command_runner)

        self.assertFalse(report.skill_loaded)
        self.assertEqual(report.skill_name, "missing-repair-skill")
        self.assertIn("You are repairing a failing local CI run", agent.prompts[0])
        self.assertFalse(report.attempts[0].skill_loaded)

    def test_bounds_large_logs_in_prompt(self):
        result = _result(1, "A" * 100 + FAILED_OUTPUT + "B" * 100)
        summary = parse_pytest_output(result.combined_output)
        config = CiFixConfig(log_limit=80)

        prompt = build_repair_prompt(config, result, summary, 1)

        self.assertIn("log characters omitted", prompt)
        self.assertLess(len(prompt), 2000)

    def test_test_command_does_not_receive_model_api_keys(self):
        from mini_claude.ci.runner import run_test_command

        with tempfile.TemporaryDirectory() as temp:
            command = (
                f'"{sys.executable}" -c "import os; '
                'print(os.environ.get(\'ANTHROPIC_API_KEY\', \'missing\')); '
                'print(os.environ.get(\'PYTHONDONTWRITEBYTECODE\', \'missing\'))"'
            )
            previous = os.environ.get("ANTHROPIC_API_KEY")
            os.environ["ANTHROPIC_API_KEY"] = "must-not-leak"
            try:
                result = run_test_command(command, Path(temp), 10)
            finally:
                if previous is None:
                    os.environ.pop("ANTHROPIC_API_KEY", None)
                else:
                    os.environ["ANTHROPIC_API_KEY"] = previous

        self.assertTrue(result.succeeded, result.combined_output)
        self.assertEqual(result.stdout.splitlines(), ["missing", "1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
