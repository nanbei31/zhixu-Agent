"""Validate an AutoCI patch against its report and checked-in policy."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from ..workspace_policy import WorkspacePolicyError, load_workspace_policy
from .models import PatchValidationResult


DEFAULT_PROTECTED_PATHS = (
    ".github/**",
    ".claude/**",
    "python/mini_claude/github/**",
    "test/**",
    "tests/**",
    "**/test/**",
    "**/tests/**",
    "test_*.py",
    "**/test_*.py",
    "*_test.py",
    "**/*_test.py",
    "pyproject.toml",
    "**/pyproject.toml",
    "setup.py",
    "**/setup.py",
    "setup.cfg",
    "**/setup.cfg",
    "tox.ini",
    "**/tox.ini",
    "requirements*.txt",
    "**/requirements*.txt",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Pipfile*",
    "**/Pipfile*",
    "poetry.lock",
    "**/poetry.lock",
    "uv.lock",
    "**/uv.lock",
)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
        raise ValueError(f"git {' '.join(args)} failed: {detail or 'unknown error'}")
    return completed


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _settings(repo: Path) -> tuple[dict, dict]:
    path = repo / ".claude" / "settings.json"
    raw = _load_json(path)
    github = raw.get("githubAutoFix", {})
    if not isinstance(github, dict):
        raise ValueError("githubAutoFix must be an object")
    return raw, github


def _paths(repo: Path, *args: str) -> list[str]:
    output = _git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", *args, "HEAD").stdout
    return [item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item]


def _numstat(repo: Path) -> tuple[int, int, bool]:
    output = _git(repo, "diff", "--cached", "--numstat", "--no-renames", "-z", "HEAD").stdout
    insertions = 0
    deletions = 0
    binary = False
    for record in output.split(b"\0"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            raise ValueError("git returned malformed numstat data")
        added, removed, _ = fields
        if added == b"-" or removed == b"-":
            binary = True
            continue
        insertions += int(added)
        deletions += int(removed)
    return insertions, deletions, binary


def _matches(path: str, patterns: tuple[str, ...]) -> str | None:
    normalized = path.replace("\\", "/").lstrip("./")
    for pattern in patterns:
        candidate = pattern.replace("\\", "/").lstrip("./")
        if fnmatch.fnmatchcase(normalized, candidate):
            return pattern
        if candidate.endswith("/**") and normalized == candidate[:-3].rstrip("/"):
            return pattern
    return None


def _config_bool(config: dict, key: str, default: bool) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"githubAutoFix.{key} must be a boolean")
    return value


def validate_and_apply_patch(
    *,
    repo: Path,
    patch_path: Path,
    report_path: Path,
    expected_base_commit: str | None = None,
) -> PatchValidationResult:
    """Validate and stage a patch; return a serializable gate result."""
    repo = repo.resolve()
    patch_path = patch_path.resolve()
    report_path = report_path.resolve()
    patch = patch_path.read_bytes()
    patch_sha = hashlib.sha256(patch).hexdigest()
    base_commit = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    result = PatchValidationResult(
        valid=False,
        base_commit=base_commit,
        patch_sha256=patch_sha,
        patch_bytes=len(patch),
    )

    try:
        if _git(repo, "status", "--porcelain", "--untracked-files=all").stdout:
            raise ValueError("repository must be clean before patch validation")
        report = _load_json(report_path)
        _, config = _settings(repo)
        policy = load_workspace_policy(repo)
        max_bytes = config.get("maxPatchBytes", 1_000_000)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
            raise ValueError("githubAutoFix.maxPatchBytes must be a positive integer")
        patterns_value = config.get("protectedPaths", list(DEFAULT_PROTECTED_PATHS))
        if not isinstance(patterns_value, list) or not all(isinstance(item, str) for item in patterns_value):
            raise ValueError("githubAutoFix.protectedPaths must be a list of strings")
        protected = tuple(patterns_value)

        if not patch:
            raise ValueError("patch is empty")
        if len(patch) > max_bytes:
            raise ValueError(f"patch exceeds configured size limit: {len(patch)} > {max_bytes}")
        if not report.get("succeeded"):
            raise ValueError("AutoCI report does not record a successful repair")
        report_base = (report.get("isolation") or {}).get("base_commit")
        expected = expected_base_commit or report_base
        if expected and base_commit != expected:
            raise ValueError(f"base commit mismatch: expected {expected}, got {base_commit}")
        if report_base and report_base != base_commit:
            raise ValueError(f"report base commit mismatch: {report_base} != {base_commit}")
        report_diff = report.get("diff") or {}
        report_sha = report_diff.get("sha256")
        if report_sha != patch_sha:
            raise ValueError(f"patch SHA256 mismatch: report={report_sha}, actual={patch_sha}")

        _git(repo, "apply", "--check", "--index", str(patch_path))
        _git(repo, "apply", "--index", str(patch_path))
        result.changed_files = _paths(repo)
        result.added_files = _paths(repo, "--diff-filter=A")
        result.deleted_files = _paths(repo, "--diff-filter=D")
        result.insertions, result.deletions, contains_binary = _numstat(repo)
        if not result.changed_files:
            raise ValueError("patch produced no staged changes")

        report_files = {
            item.get("path") for item in report_diff.get("files", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        if report_files and report_files != set(result.changed_files):
            raise ValueError("report changed-file list does not match the applied patch")
        if report_diff.get("insertions") != result.insertions or report_diff.get("deletions") != result.deletions:
            raise ValueError("report line counts do not match the applied patch")
        if result.added_files and not _config_bool(config, "allowNewFiles", True):
            raise ValueError("patch adds files but githubAutoFix.allowNewFiles is false")
        if result.deleted_files and not _config_bool(config, "allowDeletions", False):
            raise ValueError("patch deletes files but githubAutoFix.allowDeletions is false")
        if contains_binary and not _config_bool(config, "allowBinaryFiles", False):
            raise ValueError("patch contains binary changes")

        for changed in result.changed_files:
            matched = _matches(changed, protected)
            if matched:
                raise ValueError(f"protected path changed: {changed} matches {matched}")
            decision = policy.check_path(changed, write=True)
            if not decision.allowed:
                raise ValueError(f"path is outside WorkspacePolicy write scope: {changed}: {decision.reason}")
            path = repo / changed
            if path.is_symlink() and not _config_bool(config, "allowSymlinks", False):
                raise ValueError(f"symbolic link changes are not allowed: {changed}")

        result.valid = True
    except (OSError, ValueError, WorkspacePolicyError) as exc:
        result.errors.append(str(exc))
        # A failed gate must not leave a partially applied patch in the caller's
        # checkout. The repository was required to be clean before validation.
        _git(repo, "reset", "--hard", "HEAD", check=False)
        _git(repo, "clean", "-fd", check=False)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and stage an AutoCI repair patch")
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--base-commit")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = validate_and_apply_patch(
        repo=args.repo,
        patch_path=args.patch,
        report_path=args.report,
        expected_base_commit=args.base_commit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    if result.valid:
        print(f"Patch validation passed: {len(result.changed_files)} changed files")
        return
    for error in result.errors:
        print(f"Patch validation failed: {error}", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
