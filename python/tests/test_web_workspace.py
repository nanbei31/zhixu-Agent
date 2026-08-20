"""Tests for the local Web workbench core without calling a model."""

import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.agent import Agent  # noqa: E402
from mini_claude.tools import execute_tool  # noqa: E402
from mini_claude.web.events import EventBus  # noqa: E402
from mini_claude.web.task_classifier import classify_task  # noqa: E402
from mini_claude.web.workspace import WorkspaceManager  # noqa: E402


class TestWebWorkspace(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.managed = self.root / "managed"
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.manager = WorkspaceManager(self.managed)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_open_read_write_and_diff(self):
        workspace = self.manager.open_path(str(self.project))
        self.assertEqual(self.manager.read_file(workspace.id, "app.py")["content"], "VALUE = 1\n")

        self.manager.write_file(workspace.id, "app.py", "VALUE = 2\n")
        diff = self.manager.diff(workspace.id)

        self.assertEqual(diff["files"], [{"path": "app.py", "status": "modified"}])
        self.assertIn("-VALUE = 1", diff["patch"])
        self.assertIn("+VALUE = 2", diff["patch"])

    def test_path_traversal_and_sensitive_directories_are_rejected(self):
        workspace = self.manager.open_path(str(self.project))
        with self.assertRaises((ValueError, PermissionError)):
            self.manager.read_file(workspace.id, "../secret.txt")
        with self.assertRaises(PermissionError):
            self.manager.write_file(workspace.id, ".git/config", "unsafe")

        (self.project / ".env").write_text("SECRET=value\n")
        self.assertNotIn(".env", [item["path"] for item in self.manager.tree(workspace.id)])
        with self.assertRaises(PermissionError):
            self.manager.read_file(workspace.id, ".env")

    def test_browser_import_creates_managed_workspace(self):
        workspace = self.manager.import_files("demo project", [{
            "path": "src/main.py",
            "content_base64": base64.b64encode(b"print('ok')\n").decode(),
        }])
        self.assertTrue(workspace.managed)
        self.assertEqual(self.manager.read_file(workspace.id, "src/main.py")["content"], "print('ok')\n")

    def test_remove_selected_files_and_clear_access_without_deleting_source(self):
        workspace = self.manager.import_files("delete-demo", [
            {
                "path": "app.py",
                "content_base64": base64.b64encode(b"VALUE = 1\n").decode(),
            },
            {
                "path": "second.py",
                "content_base64": base64.b64encode(b"VALUE = 2\n").decode(),
            },
        ])

        result = self.manager.remove_access(workspace.id, ["second.py"])
        self.assertEqual(result["removed"], 1)
        self.assertTrue((workspace.root / "second.py").exists())
        self.assertEqual([item["path"] for item in self.manager.tree(workspace.id)], ["app.py"])
        with self.assertRaises(PermissionError):
            self.manager.read_file(workspace.id, "second.py")
        with self.assertRaises(ValueError):
            self.manager.clear_access(workspace.id, "wrong-name")

        result = self.manager.clear_access(workspace.id, workspace.name)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(self.manager.tree(workspace.id), [])
        self.assertTrue((workspace.root / "app.py").exists())

    def test_mounted_local_workspace_can_revoke_access_without_deleting_original(self):
        workspace = self.manager.open_path(str(self.project))
        result = self.manager.remove_access(workspace.id, ["app.py"])

        self.assertEqual(result["removed"], 1)
        self.assertEqual(self.manager.tree(workspace.id), [])
        self.assertTrue((self.project / "app.py").exists())
        self.assertFalse(workspace.policy().check_path("app.py", write=False).allowed)
        self.assertFalse(workspace.policy().check_path("app.py", write=True).allowed)

    def test_remove_access_rejects_traversal_and_protected_files(self):
        workspace = self.manager.open_path(str(self.project))
        with self.assertRaises((ValueError, PermissionError)):
            self.manager.remove_access(workspace.id, ["../outside.py"])
        with self.assertRaises(PermissionError):
            self.manager.remove_access(workspace.id, [".env"])

    def test_removed_file_is_hidden_from_agent_list_and_grep_tools(self):
        (self.project / "visible.py").write_text("VISIBLE_MARKER = 1\n", encoding="utf-8")
        workspace = self.manager.open_path(str(self.project))
        self.manager.remove_access(workspace.id, ["app.py"])
        policy = workspace.policy()

        direct = asyncio.run(execute_tool(
            "read_file", {"file_path": "app.py"}, workspace_policy=policy
        ))
        listed = asyncio.run(execute_tool(
            "list_files", {"path": ".", "pattern": "**/*.py"}, workspace_policy=policy
        ))
        searched = asyncio.run(execute_tool(
            "grep_search", {"path": ".", "pattern": "VALUE"}, workspace_policy=policy
        ))

        self.assertIn("Workspace policy denied", direct)
        self.assertNotIn("app.py", listed)
        self.assertIn("visible.py", listed)
        self.assertEqual(searched, "No matches found.")

    def test_exclusions_disable_agent_shell_to_prevent_bypass(self):
        workspace = self.manager.open_path(str(self.project))
        self.manager.remove_access(workspace.id, ["app.py"])

        policy = workspace.policy(allow_shell=True)

        self.assertFalse(policy.allow_agent_shell)
        self.assertNotIn("run_shell", policy.allowed_agent_tools)

    def test_relative_agent_tool_path_uses_selected_workspace(self):
        workspace = self.manager.open_path(str(self.project))
        policy = workspace.policy()
        state = {}
        read = asyncio.run(execute_tool(
            "read_file", {"file_path": "app.py"}, state, policy
        ))
        self.assertIn("VALUE = 1", read)
        result = asyncio.run(execute_tool(
            "edit_file",
            {"file_path": "app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 3"},
            state,
            policy,
        ))
        self.assertNotIn("Error", result)
        self.assertEqual((self.project / "app.py").read_text(), "VALUE = 3\n")

    def test_event_bus_and_task_classification(self):
        bus = EventBus()
        first = bus.publish("task_classified", classify_task("修复 pytest 报错"))
        second = bus.publish("tool_call_started", {"tool": "read_file"})
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(second["sequence"], 2)
        self.assertEqual(bus.after(1), [second])
        self.assertEqual(first["data"]["category"], "故障修复")

    def test_agent_checkpoint_undo_restores_modified_and_added_files(self):
        workspace = self.manager.open_path(str(self.project))
        self.manager.create_checkpoint(workspace.id)
        (self.project / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
        (self.project / "generated.py").write_text("NEW = True\n", encoding="utf-8")

        self.assertTrue(self.manager.finalize_checkpoint(workspace.id))
        self.assertTrue(self.manager.diff(workspace.id)["can_undo"])
        result = self.manager.undo_last_change(workspace.id)

        self.assertEqual((self.project / "app.py").read_text(), "VALUE = 1\n")
        self.assertFalse((self.project / "generated.py").exists())
        self.assertEqual(result["changed_files"], 2)
        self.assertFalse(result["can_undo"])

    def test_unchanged_checkpoint_is_discarded(self):
        workspace = self.manager.open_path(str(self.project))
        self.manager.create_checkpoint(workspace.id)
        self.assertFalse(self.manager.finalize_checkpoint(workspace.id))
        with self.assertRaises(ValueError):
            self.manager.undo_last_change(workspace.id)


class TestAgentTrace(unittest.IsolatedAsyncioTestCase):
    async def test_tool_wrapper_emits_real_start_and_completion_events(self):
        events = []
        agent = Agent.__new__(Agent)
        agent.event_sink = lambda event_type, data: events.append((event_type, data))
        agent._skill_allowed_tools = None
        agent._mcp_manager = type("Mcp", (), {"is_mcp_tool": lambda self, name: False})()
        agent.workspace_policy = None
        agent._read_file_state = {}
        agent.schedule_wakeup_enabled = False

        result = await agent._execute_tool_with_trace("list_files", {"path": "."})

        self.assertIsInstance(result, str)
        self.assertEqual([event[0] for event in events], [
            "tool_call_started", "tool_call_completed"
        ])
        self.assertIn("duration_ms", events[1][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
