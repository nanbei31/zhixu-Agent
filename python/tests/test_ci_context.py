"""Tests for structured AutoCI-Fix repair context."""

import sys
import unittest
from pathlib import Path

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.ci import CommandResult, RepairContext, classify_failure  # noqa: E402
from mini_claude.ci.pytest_parser import parse_pytest_output  # noqa: E402


def _result(output: str, *, exit_code: int = 1, timed_out: bool = False):
    return CommandResult(
        command="pytest -q",
        cwd="/project",
        exit_code=exit_code,
        stdout=output,
        stderr="",
        duration_seconds=0.1,
        timed_out=timed_out,
    )


class TestRepairContext(unittest.TestCase):
    def test_classifies_common_failure_types(self):
        cases = [
            ("ERROR collecting tests/test_app.py", "collection"),
            ("ModuleNotFoundError: No module named 'app'", "import"),
            ("fixture 'client' not found", "fixture"),
            ("FAILED tests/test_app.py::test_value - AssertionError", "assertion"),
            ("bash: pytest: command not found", "environment"),
        ]
        for output, expected in cases:
            with self.subTest(expected=expected):
                result = _result(output)
                summary = parse_pytest_output(output)
                self.assertEqual(classify_failure(result, summary), expected)

        timeout = _result("", exit_code=124, timed_out=True)
        self.assertEqual(classify_failure(timeout, parse_pytest_output("")), "timeout")

    def test_summary_is_compact_and_render_contains_full_evidence(self):
        output = (
            "src/app.py:8: AssertionError\n"
            "FAILED tests/test_app.py::test_value - assert 1 == 2\n"
            "1 failed in 0.10s\n"
        )
        result = _result(output)
        summary = parse_pytest_output(output)
        context = RepairContext(
            attempt=2,
            max_attempts=3,
            test_command="pytest -q",
            result=result,
            summary=summary,
            targets=("src/app.py",),
            writable_paths=("src",),
            previous_attempts=({"number": 1, "exit_code": 1, "result": "failed"},),
        )

        compact = context.summary_dict()
        self.assertEqual(compact["classification"], "assertion")
        self.assertEqual(compact["previous_attempts"][0]["number"], 1)
        self.assertNotIn("raw_log", compact)

        rendered = context.render(log_limit=16000)
        self.assertIn("tests/test_app.py::test_value", rendered)
        self.assertIn("src/app.py", rendered)
        self.assertIn("第 1 轮", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
