"""Tests for local AutoCI run artifacts and aggregate observability storage."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.ci.models import (  # noqa: E402
    CiFixReport,
    CommandResult,
    PytestSummary,
)
from mini_claude.ci.storage import (  # noqa: E402
    EventRecorder,
    create_run_paths,
    get_data_root,
    index_run,
    list_recent_runs,
    usage_summary,
    write_run_artifacts,
)


def _report(run_id: str, project_id: str) -> CiFixReport:
    result = CommandResult(
        command="pytest -q",
        cwd="/project",
        exit_code=0,
        stdout="2 passed",
        stderr="",
        duration_seconds=0.5,
    )
    return CiFixReport(
        initial=result,
        initial_summary=PytestSummary(passed=2),
        run_id=run_id,
        project_id=project_id,
        skill_name="pytest-repair",
        skill_loaded=True,
        usage={
            "model": "fake-model",
            "provider": "fake-provider",
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 5,
            "cache_creation_tokens": 1,
        },
        cost={"estimated_usd": 0.004},
        timing={
            "started_at": "2026-08-11T00:00:00+00:00",
            "finished_at": "2026-08-11T00:00:02+00:00",
            "total_duration_seconds": 2.0,
            "agent_duration_seconds": 1.0,
            "test_duration_seconds": 0.5,
        },
        diff={"changed_file_count": 1, "insertions": 3, "deletions": 1},
        isolation={"base_commit": "abc123"},
    )


class TestEventRecorder(unittest.TestCase):
    def test_buffers_events_until_run_directory_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            recorder = EventRecorder("run-1")
            recorder.emit("run_started", {"mode": "test"})
            path = recorder.bind(run_dir)
            recorder.emit("run_finished", {"succeeded": True})

            events = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([event["sequence"] for event in events], [1, 2])
            self.assertEqual([event["type"] for event in events], [
                "run_started",
                "run_finished",
            ])
            self.assertTrue(all(event["run_id"] == "run-1" for event in events))


class TestRunStorage(unittest.TestCase):
    def test_writes_artifacts_and_indexes_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp) / "data"
            project_root = Path(temp) / "project"
            project_root.mkdir()
            with patch.dict(
                os.environ,
                {"MINI_CLAUDE_DATA_DIR": str(data_root)},
            ):
                self.assertEqual(get_data_root(), data_root.resolve())
                paths = create_run_paths(project_root, "run-1")
                report = _report(paths.run_id, paths.project_id)
                write_run_artifacts(
                    paths.run_dir,
                    report=report,
                    metadata={"run_id": paths.run_id},
                    patch="diff --git a/app.py b/app.py\n",
                )
                index_run(
                    report,
                    project_root=project_root,
                    artifact_dir=paths.run_dir,
                    database_path=paths.database_path,
                )

                recent = list_recent_runs(limit=1)
                summary = usage_summary()

                self.assertEqual(recent[0]["run_id"], "run-1")
                self.assertEqual(recent[0]["input_tokens"], 100)
                self.assertAlmostEqual(recent[0]["estimated_cost_usd"], 0.004)
                self.assertEqual(summary["run_count"], 1)
                self.assertEqual(summary["passed_count"], 1)
                self.assertEqual(summary["input_tokens"], 100)
                self.assertEqual(summary["output_tokens"], 20)
                self.assertEqual(summary["cache_creation_tokens"], 1)
                self.assertAlmostEqual(summary["estimated_cost_usd"], 0.004)
                self.assertTrue((paths.run_dir / "report.json").is_file())
                self.assertTrue((paths.run_dir / "changes.patch").is_file())
                self.assertTrue(paths.database_path.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
