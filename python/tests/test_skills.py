"""Tests for YAML Skill parsing, discovery, and runtime permissions."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.agent import Agent  # noqa: E402
from mini_claude.skills import (  # noqa: E402
    SkillValidationError,
    discover_skills,
    execute_skill,
    reset_skill_cache,
)


VALID_SKILL = """---
name: pytest-repair
description: 分析 pytest 失败并进行最小、安全的生产代码修复
when-to-use: 当 pytest 命令失败时使用
user-invocable: true
context: inline
allowed-tools: [read_file, edit_file]
---

# Pytest 故障修复

失败上下文：$ARGUMENTS
目录：${CLAUDE_SKILL_DIR}
"""


class TestSkills(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.user_home = self.root / "home"
        self.user_home.mkdir()
        self.project = self.root / "project"
        self.nested = self.project / "examples" / "demo"
        self.nested.mkdir(parents=True)
        self.old_cwd = Path.cwd()
        os.chdir(self.nested)
        self.home_patch = patch("mini_claude.skills.Path.home", return_value=self.user_home)
        self.home_patch.start()
        reset_skill_cache()

    def tearDown(self):
        reset_skill_cache()
        self.home_patch.stop()
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def write_skill(self, base: Path, name: str, content: str) -> Path:
        directory = base / ".claude" / "skills" / name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "SKILL.md"
        path.write_text(content, encoding="utf-8")
        return path

    def test_discovers_yaml_skill_from_ancestor_and_resolves_variables(self):
        skill_file = self.write_skill(self.project, "pytest-repair", VALID_SKILL)

        skills = discover_skills()
        self.assertEqual([skill.name for skill in skills], ["pytest-repair"])
        skill = skills[0]
        self.assertEqual(skill.allowed_tools, ("read_file", "edit_file"))
        self.assertTrue(skill.user_invocable)
        self.assertIn("pytest", skill.description)

        result = execute_skill("pytest-repair", "1 failed")
        self.assertIn("失败上下文：1 failed", result["prompt"])
        self.assertIn(str(skill_file.parent.resolve()), result["prompt"])

    def test_accepts_legacy_underscore_aliases(self):
        content = VALID_SKILL.replace("user-invocable", "user_invocable").replace(
            "allowed-tools", "allowed_tools"
        )
        self.write_skill(self.project, "pytest-repair", content)

        skill = discover_skills()[0]
        self.assertTrue(skill.user_invocable)
        self.assertEqual(skill.allowed_tools, ("read_file", "edit_file"))

    def test_normalizes_canonical_string_boolean(self):
        content = VALID_SKILL.replace(
            "user-invocable: true", "user-invocable: false"
        )
        self.write_skill(self.project, "pytest-repair", content)

        self.assertFalse(discover_skills()[0].user_invocable)

    def test_normalizes_legacy_string_boolean(self):
        content = VALID_SKILL.replace(
            "user-invocable: true", 'user_invocable: " false "'
        )
        self.write_skill(self.project, "pytest-repair", content)

        self.assertFalse(discover_skills()[0].user_invocable)

    def test_rejects_invalid_yaml_and_schema(self):
        self.write_skill(
            self.project,
            "broken",
            "---\nname: [broken\ndescription: bad\n---\nbody\n",
        )
        with self.assertRaisesRegex(SkillValidationError, "invalid YAML"):
            discover_skills()

        (self.project / ".claude" / "skills" / "broken").rename(
            self.project / ".claude" / "skills" / "broken-disabled"
        )
        broken_file = self.project / ".claude" / "skills" / "broken-disabled" / "SKILL.md"
        broken_file.write_text(
            "---\nname: broken\ndescription: bad\nallowed-tools: read_file\n---\nbody\n",
            encoding="utf-8",
        )
        reset_skill_cache()
        with self.assertRaisesRegex(SkillValidationError, "allowed-tools"):
            discover_skills()

    def test_project_skill_overrides_user_skill(self):
        user_content = VALID_SKILL.replace("生产代码修复", "用户级版本")
        self.write_skill(self.user_home, "pytest-repair", user_content)
        self.write_skill(self.project, "pytest-repair", VALID_SKILL)

        skill = discover_skills()[0]
        self.assertEqual(skill.source, "project")
        self.assertIn("生产代码修复", skill.description)


class TestSkillRuntimePermissions(unittest.IsolatedAsyncioTestCase):
    async def test_interactive_agent_resets_skill_scope_between_turns(self):
        agent = Agent.__new__(Agent)
        agent.enable_mcp = False
        agent.is_sub_agent = True
        agent.use_openai = False
        observed_scopes = []

        async def fake_chat(_message):
            observed_scopes.append(agent._skill_allowed_tools)

        agent._chat_anthropic = fake_chat
        for allowed in ({"read_file"}, {"grep_search", "edit_file"}):
            agent._skill_allowed_tools = frozenset(allowed)
            await agent.chat("run one interactive Skill turn")
            self.assertIsNone(agent._skill_allowed_tools)

        self.assertEqual(
            observed_scopes,
            [
                frozenset({"read_file"}),
                frozenset({"grep_search", "edit_file"}),
            ],
        )

    async def test_inline_skill_activation_installs_runtime_allowlist(self):
        agent = Agent.__new__(Agent)
        agent._skill_allowed_tools = None
        with patch(
            "mini_claude.skills.execute_skill",
            return_value={
                "prompt": "repair safely",
                "allowed_tools": ("read_file", "edit_file"),
                "context": "inline",
            },
        ):
            result = await agent._execute_skill_tool(
                {"skill_name": "pytest-repair", "args": "1 failed"}
            )

        self.assertIn("pytest-repair", result)
        self.assertEqual(
            agent._skill_allowed_tools,
            frozenset({"read_file", "edit_file"}),
        )

    async def test_executor_denies_tool_outside_active_skill_allowlist(self):
        agent = Agent.__new__(Agent)
        agent._skill_allowed_tools = frozenset({"read_file"})

        result = await agent._execute_tool_call(
            "write_file", {"file_path": "should-not-exist.py", "content": "unsafe = True"}
        )

        self.assertIn("Skill runtime permission denied", result)
        self.assertFalse(Path("should-not-exist.py").exists())

    def test_runtime_tool_schemas_are_filtered(self):
        agent = Agent.__new__(Agent)
        agent.tools = [
            {"name": "read_file"},
            {"name": "write_file"},
            {"name": "run_shell"},
        ]
        agent._skill_allowed_tools = frozenset({"read_file", "write_file"})

        self.assertEqual(
            [tool["name"] for tool in agent._runtime_tools()],
            ["read_file", "write_file"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
