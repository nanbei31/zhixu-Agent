"""Security-boundary tests for persistent workspace policies."""

import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.workspace_policy import (  # noqa: E402
    WorkspacePolicy,
    WorkspacePolicyError,
    load_workspace_policy,
)
from mini_claude.tools import check_permission, execute_tool  # noqa: E402
from mini_claude.__main__ import _ci_tool_definitions  # noqa: E402


class TestWorkspacePolicy(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / ".claude").mkdir()
        (self.root / "src" / "app.py").write_text("VALUE = 1\n")
        (self.root / "tests" / "test_app.py").write_text("def test_app(): pass\n")
        (self.root / ".env").write_text("SECRET=x\n")
        settings = {
            "workspacePolicy": {
                "readablePaths": ["."],
                "writablePaths": ["src/"],
                "denyPaths": ["private/"],
                "agentTools": [
                    "read_file", "list_files", "grep_search", "write_file", "edit_file"
                ],
                "allowAgentShell": False,
            }
        }
        (self.root / ".claude" / "settings.json").write_text(json.dumps(settings))

    def tearDown(self):
        self.temp_dir.cleanup()

    def load(self, **kwargs) -> WorkspacePolicy:
        return load_workspace_policy(self.root, **kwargs)

    def test_project_root_is_inferred_from_settings_location(self):
        policy = self.load()
        self.assertEqual(policy.project_root, self.root)

        settings_path = self.root / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["workspacePolicy"]["projectRoot"] = "."
        settings_path.write_text(json.dumps(settings))
        self.assertEqual(self.load().project_root, self.root)

    def test_project_root_cannot_widen_settings_boundary(self):
        settings_path = self.root / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["workspacePolicy"]["projectRoot"] = ".."
        settings_path.write_text(json.dumps(settings))
        with self.assertRaisesRegex(WorkspacePolicyError, "must resolve"):
            self.load()

    def test_reads_project_but_writes_only_configured_root(self):
        policy = self.load()
        self.assertTrue(policy.check_tool_call(
            "read_file", {"file_path": "tests/test_app.py"}
        ).allowed)
        self.assertTrue(policy.check_tool_call(
            "edit_file", {"file_path": "src/app.py"}
        ).allowed)
        self.assertFalse(policy.check_tool_call(
            "edit_file", {"file_path": "tests/test_app.py"}
        ).allowed)

    def test_rejects_traversal_and_sensitive_files(self):
        policy = self.load()
        self.assertFalse(policy.check_tool_call(
            "read_file", {"file_path": "../outside.py"}
        ).allowed)
        self.assertFalse(policy.check_tool_call(
            "read_file", {"file_path": ".env"}
        ).allowed)
        self.assertFalse(policy.check_tool_call(
            "read_file", {"file_path": ".git/config"}
        ).allowed)

    def test_cli_path_can_only_narrow_policy(self):
        (self.root / "src" / "orders").mkdir()
        policy = self.load(cli_allowed_paths=("src/orders",))
        self.assertTrue(policy.check_tool_call(
            "write_file", {"file_path": "src/orders/new.py"}
        ).allowed)
        self.assertFalse(policy.check_tool_call(
            "edit_file", {"file_path": "src/app.py"}
        ).allowed)
        with self.assertRaises(WorkspacePolicyError):
            self.load(cli_allowed_paths=("tests",))

    def test_targets_are_validated_but_do_not_grant_write_access(self):
        policy = self.load(targets=("tests/test_app.py",))
        self.assertEqual(policy.relative_targets(), ("tests/test_app.py",))
        self.assertFalse(policy.check_tool_call(
            "edit_file", {"file_path": "tests/test_app.py"}
        ).allowed)
        with self.assertRaisesRegex(WorkspacePolicyError, "target is blocked"):
            self.load(targets=(".env",))

    def test_shell_and_unlisted_tools_are_denied(self):
        policy = self.load()
        self.assertFalse(policy.check_tool_call(
            "run_shell", {"command": "sed -i s/a/b/ src/app.py"}
        ).allowed)
        self.assertFalse(policy.check_tool_call("agent", {}).allowed)
        self.assertFalse(policy.check_tool_call("skill", {}).allowed)

    def test_ci_never_exposes_skill_tool_even_if_policy_lists_it(self):
        policy = self.load()
        permissive_metadata = replace(
            policy,
            allowed_agent_tools=policy.allowed_agent_tools | {"skill"},
        )

        names = {tool["name"] for tool in _ci_tool_definitions(permissive_metadata)}

        self.assertNotIn("skill", names)
        self.assertIn("read_file", names)

    def test_yolo_cannot_bypass_and_executor_checks_again(self):
        policy = self.load()
        permission = check_permission(
            "edit_file",
            {"file_path": "tests/test_app.py"},
            "bypassPermissions",
            workspace_policy=policy,
        )
        self.assertEqual(permission["action"], "deny")

        blocked_file = self.root / "tests" / "created.py"
        result = asyncio.run(execute_tool(
            "write_file",
            {"file_path": str(blocked_file), "content": "unsafe = True\n"},
            workspace_policy=policy,
        ))
        self.assertIn("Workspace policy denied", result)
        self.assertFalse(blocked_file.exists())

    def test_relative_tool_paths_use_invocation_directory(self):
        policy = load_workspace_policy(
            self.root / "tests",
            cli_allowed_paths=("../src",),
            targets=("test_app.py",),
        )
        self.assertTrue(policy.check_tool_call(
            "edit_file", {"file_path": "../src/app.py"}
        ).allowed)
        self.assertFalse(policy.check_tool_call(
            "edit_file", {"file_path": "test_app.py"}
        ).allowed)

    def test_symlink_escape_is_denied(self):
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir(exist_ok=True)
        link = self.root / "src" / "outside-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation is not available")
        try:
            policy = self.load()
            self.assertFalse(policy.check_tool_call(
                "write_file", {"file_path": "src/outside-link/new.py"}
            ).allowed)
        finally:
            link.unlink(missing_ok=True)
            outside.rmdir()


if __name__ == "__main__":
    unittest.main(verbosity=2)
