"""Tests for the pytest terminal-output parser."""

import sys
import unittest
from pathlib import Path

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.ci import parse_pytest_output  # noqa: E402


class TestPytestParser(unittest.TestCase):
    def test_extracts_failure_node_message_location_and_counts(self):
        output = """
============================= test session starts =============================
_______________________________ test_add ____________________________________

    def test_add():
>       assert add(2, 3) == 5
E       assert -1 == 5

calculator_test.py:6: AssertionError
=========================== short test summary info ===========================
FAILED calculator_test.py::test_add - assert -1 == 5
========================= 1 failed, 2 passed in 0.12s =========================
"""

        summary = parse_pytest_output(output)

        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.passed, 2)
        self.assertEqual(summary.duration_seconds, 0.12)
        self.assertEqual(summary.locations, ("calculator_test.py:6",))
        self.assertEqual(len(summary.failures), 1)
        failure = summary.failures[0]
        self.assertEqual(failure.node_id, "calculator_test.py::test_add")
        self.assertEqual(failure.file_path, "calculator_test.py")
        self.assertEqual(failure.test_name, "test_add")
        self.assertEqual(failure.message, "assert -1 == 5")

    def test_strips_ansi_and_parses_collection_errors(self):
        output = (
            "\x1b[31mERROR tests/test_import.py\x1b[0m\n"
            "tests/test_import.py:2: in <module>\n"
            "    import missing_package\n"
            "E   ModuleNotFoundError: No module named 'missing_package'\n"
            "==================== 1 error in 0.08s ====================\n"
        )

        summary = parse_pytest_output(output)

        self.assertEqual(summary.errors, 1)
        self.assertEqual(summary.duration_seconds, 0.08)
        self.assertEqual(summary.locations, ("tests/test_import.py:2",))
        self.assertEqual(summary.failures, ())

    def test_returns_empty_summary_for_non_pytest_output(self):
        summary = parse_pytest_output("command not found: pytest")

        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.errors, 0)
        self.assertIn("could not be summarized", summary.headline)


if __name__ == "__main__":
    unittest.main(verbosity=2)
