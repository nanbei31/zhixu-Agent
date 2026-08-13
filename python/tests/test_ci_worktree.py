"""Integration tests for cumulative Git worktree repair and rollback."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.ci import CiFixConfig, WorktreeError, WorktreeSession  # noqa: E402
from mini_claude.ci.workflow import run_ci_fix_workflow  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return completed.stdout.strip()


def _init_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / ".claude").mkdir()
    skill_dir = root / ".claude" / "skills" / "pytest-repair"
    skill_dir.mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "check.py").write_text(
        "from pathlib import Path\n"
        "value = Path('src/app.py').read_text(encoding='utf-8').strip()\n"
        "raise SystemExit(0 if value == 'VALUE = 3' else 1)\n",
        encoding="utf-8",
    )
    settings = {
        "workspacePolicy": {
            "readablePaths": ["."],
            "writablePaths": ["src/"],
            "denyPaths": [".git/", ".env"],
            "agentTools": [
                "read_file", "list_files", "grep_search", "edit_file", "write_file"
            ],
            "allowAgentShell": False,
        }
    }
    (root / ".claude" / "settings.json").write_text(
        json.dumps(settings), encoding="utf-8"
    )
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: pytest-repair\n"
        "description: Repair pytest failures\n"
        "user-invocable: true\n"
        "context: inline\n"
        "allowed-tools: [read_file, edit_file, write_file, run_shell]\n"
        "---\n\nRepair this failure:\n\n$ARGUMENTS\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(".autoci/\n__pycache__/\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.email", "autoci@example.invalid")
    _git(root, "config", "user.name", "AutoCI Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")


class EditingAgent:
    def __init__(self, policy, values=(2, 3)):
        self.policy = policy
        self.values = iter(values)
        self.prompts = []
        self.closed = False

    async def chat(self, user_message):
        self.prompts.append(user_message)
        value = next(self.values)
        (self.policy.project_root / "src" / "app.py").write_text(
            f"VALUE = {value}\n", encoding="utf-8"
        )

    async def close(self):
        self.closed = True

    def get_usage_metrics(self):
        attempts = len(self.prompts)
        return {
            "model": "fake-repair-model",
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


class CrashingAgent(EditingAgent):
    async def chat(self, user_message):
        await super().chat(user_message)
        raise RuntimeError("model failed during repair")


class TestWorktreeSession(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.artifacts = self.root.parent / f"{self.root.name}-artifacts"
        _init_repo(self.root)

    def tearDown(self):
        subprocess.run(
            ["git", "worktree", "prune"], cwd=self.root, capture_output=True
        )
        if self.artifacts.exists():
            shutil.rmtree(self.artifacts)
        self.temp_dir.cleanup()

    def test_maps_invocation_subdirectory_and_isolates_changes(self):
        invocation = self.root / "src"
        session = WorktreeSession.create(
            invocation, artifacts_dir=self.artifacts
        )
        try:
            self.assertEqual(session.execution_cwd, session.worktree_root / "src")
            (session.worktree_root / "src" / "app.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            (session.worktree_root / "src" / "new.py").write_text(
                "NEW = True\n", encoding="utf-8"
            )
            snapshot = session.snapshot()
            self.assertIn("src/app.py", snapshot.status)
            self.assertIn("src/new.py", snapshot.status)
            self.assertIn("+VALUE = 2", snapshot.patch)
            self.assertIn("+NEW = True", snapshot.patch)
            self.assertEqual(
                (self.root / "src" / "app.py").read_text(encoding="utf-8"),
                "VALUE = 1\n",
            )
        finally:
            session.cleanup()
        self.assertFalse(session.worktree_root.exists())

    def test_rejects_dirty_repository(self):
        (self.root / "src" / "app.py").write_text("VALUE = 9\n", encoding="utf-8")
        with self.assertRaisesRegex(WorktreeError, "clean repository"):
            WorktreeSession.create(self.root)


class TestCiWorktreeWorkflow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.artifacts = self.root.parent / f"{self.root.name}-artifacts"
        _init_repo(self.root)

    def tearDown(self):
        worktrees = _git(self.root, "worktree", "list", "--porcelain")
        paths = [
            Path(line.removeprefix("worktree "))
            for line in worktrees.splitlines()
            if line.startswith("worktree ")
        ]
        for path in paths:
            if path.resolve() != self.root:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(path)],
                    cwd=self.root,
                    capture_output=True,
                )
        subprocess.run(["git", "worktree", "prune"], cwd=self.root, capture_output=True)
        if self.artifacts.exists():
            shutil.rmtree(self.artifacts)
        self.temp_dir.cleanup()

    def _config(self, attempts=2):
        return CiFixConfig(
            test_command=f'"{sys.executable}" check.py',
            cwd=self.root,
            max_attempts=attempts,
            timeout_seconds=10,
        )

    async def test_attempts_accumulate_and_success_leaves_source_unchanged(self):
        agents = []

        def factory(policy):
            agent = EditingAgent(policy, values=(2, 3))
            agents.append(agent)
            return agent

        result = await run_ci_fix_workflow(
            config=self._config(),
            agent_factory=factory,
            targets=("src/app.py",),
            artifacts_dir=self.artifacts,
        )

        self.assertTrue(result.report.succeeded)
        self.assertEqual(len(agents), 1)
        self.assertEqual(len(result.report.attempts), 2)
        self.assertEqual(len(agents[0].prompts), 2)
        self.assertNotIn("$ARGUMENTS", agents[0].prompts[0])
        self.assertIn("AutoCI-Fix 结构化修复上下文", agents[0].prompts[0])
        self.assertIn("Repair this failure:", agents[0].prompts[0])
        self.assertTrue(agents[0].closed)
        self.assertNotEqual(agents[0].policy.project_root, self.root)
        self.assertEqual(
            agents[0].policy.project_root,
            Path(result.report.isolation["worktree_root"]),
        )
        self.assertFalse(result.worktree_preserved)
        self.assertEqual(
            (self.root / "src" / "app.py").read_text(encoding="utf-8"),
            "VALUE = 1\n",
        )
        patch = (result.artifact_dir / "changes.patch").read_text(encoding="utf-8")
        self.assertIn("+VALUE = 3", patch)
        metadata = json.loads(
            (result.artifact_dir / "metadata.json").read_text(encoding="utf-8")
        )
        self.assertFalse(metadata["rolled_back"])
        self.assertTrue(result.report.skill_loaded)
        report_data = json.loads(
            (result.artifact_dir / "report.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report_data["skill_name"], "pytest-repair")
        self.assertTrue(report_data["skill_loaded"])
        self.assertEqual(report_data["usage"]["input_tokens"], 200)
        self.assertEqual(report_data["usage"]["output_tokens"], 40)
        self.assertAlmostEqual(report_data["cost"]["estimated_usd"], 0.004)
        self.assertGreaterEqual(report_data["timing"]["total_duration_seconds"], 0.0)
        self.assertEqual(report_data["diff"]["changed_file_count"], 1)
        self.assertEqual(report_data["diff"]["insertions"], 1)
        self.assertEqual(report_data["diff"]["deletions"], 1)
        self.assertEqual(report_data["attempts"][0]["usage"]["input_tokens"], 100)
        self.assertEqual(
            report_data["attempts"][0]["context_summary"]["classification"],
            "unknown",
        )
        events = [
            json.loads(line)
            for line in (result.artifact_dir / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(
            [event["sequence"] for event in events],
            list(range(1, len(events) + 1)),
        )
        event_types = {event["type"] for event in events}
        self.assertIn("run_started", event_types)
        self.assertIn("agent_attempt_finished", event_types)
        self.assertIn("diff_captured", event_types)
        self.assertIn("run_finished", event_types)
        self.assertTrue((self.artifacts / "autoci.db").is_file())
        self.assertFalse(
            agents[0].policy.check_tool_call(
                "run_shell", {"command": "touch src/unsafe.py"}
            ).allowed
        )
        self.assertFalse(Path(metadata["worktree_root"]).exists())

    async def test_final_failure_rolls_back_cumulative_changes(self):
        result = await run_ci_fix_workflow(
            config=self._config(),
            agent_factory=lambda policy: EditingAgent(policy, values=(2, 2)),
            artifacts_dir=self.artifacts,
        )

        self.assertFalse(result.report.succeeded)
        self.assertEqual(len(result.report.attempts), 2)
        self.assertEqual(
            (self.root / "src" / "app.py").read_text(encoding="utf-8"),
            "VALUE = 1\n",
        )
        metadata = json.loads(
            (result.artifact_dir / "metadata.json").read_text(encoding="utf-8")
        )
        self.assertTrue(metadata["rolled_back"])
        self.assertFalse(Path(metadata["worktree_root"]).exists())
        self.assertTrue((result.artifact_dir / "final-test.log").exists())

    async def test_can_preserve_failed_worktree_for_debugging(self):
        result = await run_ci_fix_workflow(
            config=self._config(attempts=1),
            agent_factory=lambda policy: EditingAgent(policy, values=(2,)),
            keep_failed_worktree=True,
            artifacts_dir=self.artifacts,
        )

        self.assertFalse(result.report.succeeded)
        self.assertTrue(result.worktree_preserved)
        self.assertTrue(result.worktree_root.exists())
        self.assertEqual(
            (result.worktree_root / "src" / "app.py").read_text(encoding="utf-8"),
            "VALUE = 2\n",
        )

    async def test_exception_saves_evidence_and_removes_worktree(self):
        with self.assertRaisesRegex(RuntimeError, "model failed"):
            await run_ci_fix_workflow(
                config=self._config(attempts=1),
                agent_factory=lambda policy: CrashingAgent(policy, values=(2,)),
                artifacts_dir=self.artifacts,
            )

        run_dirs = list(self.artifacts.iterdir())
        self.assertEqual(len(run_dirs), 1)
        metadata = json.loads(
            (run_dirs[0] / "metadata.json").read_text(encoding="utf-8")
        )
        self.assertTrue(metadata["rolled_back"])
        self.assertFalse(Path(metadata["worktree_root"]).exists())
        self.assertIn(
            "model failed during repair",
            (run_dirs[0] / "error.txt").read_text(encoding="utf-8"),
        )

    async def test_missing_skill_is_recorded_without_exposing_skill_tool(self):
        config = CiFixConfig(
            test_command=f'"{sys.executable}" check.py',
            cwd=self.root,
            max_attempts=2,
            timeout_seconds=10,
            repair_skill_name="missing-skill",
        )
        result = await run_ci_fix_workflow(
            config=config,
            agent_factory=lambda policy: EditingAgent(policy, values=(2, 3)),
            artifacts_dir=self.artifacts,
        )

        self.assertTrue(result.report.succeeded)
        self.assertEqual(result.report.skill_name, "missing-skill")
        self.assertFalse(result.report.skill_loaded)


if __name__ == "__main__":
    unittest.main(verbosity=2)
