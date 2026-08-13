"""Tests for the deterministic GitHub Actions AutoCI publishing gate."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.github.pr_report import render_pr_body  # noqa: E402
from mini_claude.github.validate_patch import validate_and_apply_patch  # noqa: E402


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def _settings(*, allow_deletions: bool = False) -> dict:
    return {
        "workspacePolicy": {
            "readablePaths": ["."],
            "writablePaths": ["src/"],
            "denyPaths": [".git/", ".env"],
            "agentTools": ["read_file", "list_files", "grep_search", "edit_file", "write_file"],
            "allowAgentShell": False,
        },
        "githubAutoFix": {
            "protectedPaths": [
                ".github/**", ".claude/**", "python/mini_claude/github/**",
                "tests/**", "**/test_*.py",
            ],
            "maxPatchBytes": 100000,
            "allowNewFiles": True,
            "allowDeletions": allow_deletions,
            "allowSymlinks": False,
            "allowBinaryFiles": False,
        },
    }


class AutoFixRepo:
    def __init__(self, temp: str):
        self.root = Path(temp)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / ".claude").mkdir()
        (self.root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "tests" / "test_app.py").write_text("def test_value(): pass\n", encoding="utf-8")
        (self.root / ".claude" / "settings.json").write_text(
            json.dumps(_settings()), encoding="utf-8"
        )
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "autoci@example.invalid")
        _git(self.root, "config", "user.name", "AutoCI Test")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "base")
        self.base_commit = _git(self.root, "rev-parse", "HEAD")

    def make_handoff(self, changes: dict[str, str | None]) -> tuple[Path, Path]:
        for relative, content in changes.items():
            path = self.root / relative
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        _git(self.root, "add", "-A")
        patch = self.root.parent / f"{self.root.name}-changes.patch"
        patch_bytes = subprocess.run(
            ["git", "diff", "--cached", "--binary", "--full-index", "HEAD"],
            cwd=self.root,
            capture_output=True,
            check=True,
        ).stdout
        patch.write_bytes(patch_bytes)
        numstat = _git(self.root, "diff", "--cached", "--numstat", "--no-renames", "HEAD")
        files = []
        insertions = deletions = 0
        for line in numstat.splitlines():
            added, removed, relative = line.split("\t", 2)
            if added != "-":
                insertions += int(added)
                deletions += int(removed)
            files.append({"path": relative})
        report = {
            "succeeded": True,
            "isolation": {"base_commit": self.base_commit},
            "diff": {
                "sha256": hashlib.sha256(patch_bytes).hexdigest(),
                "changed_file_count": len(files),
                "insertions": insertions,
                "deletions": deletions,
                "files": files,
            },
            "initial": {"exit_code": 1},
            "final": {"exit_code": 0},
            "attempts": [{}],
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "cost": {"estimated_usd": 0.002},
            "timing": {"total_duration_seconds": 3.5},
            "skill_name": "pytest-repair",
            "workspace_policy": {"writable_paths": [str(self.root / "src")]},
        }
        report_path = self.root.parent / f"{self.root.name}-report.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        _git(self.root, "reset", "--hard", "HEAD")
        for relative in changes:
            path = self.root / relative
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", relative],
                cwd=self.root,
                capture_output=True,
            ).returncode == 0
            if path.exists() and not tracked:
                path.unlink()
        return patch, report_path


class TestPatchValidation(unittest.TestCase):
    def test_valid_patch_is_staged(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = AutoFixRepo(temp)
            patch, report = fixture.make_handoff({"src/app.py": "VALUE = 2\n"})

            result = validate_and_apply_patch(
                repo=fixture.root,
                patch_path=patch,
                report_path=report,
                expected_base_commit=fixture.base_commit,
            )

            self.assertTrue(result.valid, result.errors)
            self.assertEqual(result.changed_files, ["src/app.py"])
            self.assertEqual(result.insertions, 1)
            self.assertEqual(result.deletions, 1)
            self.assertEqual(_git(fixture.root, "diff", "--cached", "--name-only"), "src/app.py")

    def test_rejects_patch_sha_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = AutoFixRepo(temp)
            patch, report = fixture.make_handoff({"src/app.py": "VALUE = 2\n"})
            data = json.loads(report.read_text(encoding="utf-8"))
            data["diff"]["sha256"] = "0" * 64
            report.write_text(json.dumps(data), encoding="utf-8")

            result = validate_and_apply_patch(repo=fixture.root, patch_path=patch, report_path=report)

            self.assertFalse(result.valid)
            self.assertIn("SHA256 mismatch", result.errors[0])

    def test_rejects_test_file_change(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = AutoFixRepo(temp)
            patch, report = fixture.make_handoff({"tests/test_app.py": "def test_value(): assert False\n"})

            result = validate_and_apply_patch(repo=fixture.root, patch_path=patch, report_path=report)

            self.assertFalse(result.valid)
            self.assertTrue(
                "protected path changed" in result.errors[0]
                or "outside WorkspacePolicy" in result.errors[0]
            )
            self.assertEqual(_git(fixture.root, "status", "--porcelain"), "")

    def test_rejects_file_outside_workspace_policy_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = AutoFixRepo(temp)
            patch, report = fixture.make_handoff({"outside.py": "VALUE = 2\n"})

            result = validate_and_apply_patch(repo=fixture.root, patch_path=patch, report_path=report)

            self.assertFalse(result.valid)
            self.assertIn("outside WorkspacePolicy", result.errors[0])
            self.assertFalse((fixture.root / "outside.py").exists())
            self.assertEqual(_git(fixture.root, "status", "--porcelain"), "")

    def test_rejects_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = AutoFixRepo(temp)
            patch, report = fixture.make_handoff({"src/app.py": None})

            result = validate_and_apply_patch(repo=fixture.root, patch_path=patch, report_path=report)

            self.assertFalse(result.valid)
            self.assertIn("deletes files", result.errors[0])


class TestPrReport(unittest.TestCase):
    def test_renders_bounded_structured_body(self):
        report = {
            "initial": {"exit_code": 1},
            "final": {"exit_code": 0},
            "attempts": [{}, {}],
            "diff": {"changed_file_count": 1, "insertions": 3, "deletions": 1},
            "usage": {"input_tokens": 120, "output_tokens": 30},
            "cost": {"estimated_usd": 0.004},
            "timing": {"total_duration_seconds": 4.25},
            "skill_name": "pytest-repair",
            "workspace_policy": {"writable_paths": ["/repo/src"]},
        }
        validation = {"changed_files": ["src/app.py"], "patch_sha256": "abc123"}

        body = render_pr_body(report, validation, run_url="https://github.example/run/1")

        self.assertIn("Draft PR", body)
        self.assertIn("`src/app.py`", body)
        self.assertIn("120", body)
        self.assertIn("$0.004000", body)
        self.assertIn("https://github.example/run/1", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
