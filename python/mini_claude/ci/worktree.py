"""Git worktree isolation and artifact capture for AutoCI-Fix."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import CiFixReport
from .storage import default_runs_base, generate_run_id, write_run_artifacts


class WorktreeError(RuntimeError):
    """Raised when an isolated AutoCI-Fix workspace cannot be prepared."""


def _run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
        raise WorktreeError(f"git {' '.join(args)} failed: {detail}")
    return completed


def _find_settings(start: Path, repo_root: Path) -> Path | None:
    for directory in (start, *start.parents):
        try:
            directory.relative_to(repo_root)
        except ValueError:
            break
        candidate = directory / ".claude" / "settings.json"
        if candidate.is_file():
            return candidate
        if directory == repo_root:
            break
    return None


@dataclass(frozen=True)
class WorktreeSnapshot:
    status: str
    patch: str
    numstat: str = ""

    def diff_summary(self) -> dict:
        files = []
        insertions = 0
        deletions = 0
        for line in self.numstat.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, removed, path = parts
            binary = added == "-" or removed == "-"
            added_count = None if binary else int(added)
            removed_count = None if binary else int(removed)
            if added_count is not None:
                insertions += added_count
            if removed_count is not None:
                deletions += removed_count
            files.append({
                "path": path,
                "insertions": added_count,
                "deletions": removed_count,
                "binary": binary,
            })
        patch_bytes = self.patch.encode("utf-8")
        return {
            "available": True,
            "artifact": "changes.patch",
            "changed_file_count": len(files),
            "insertions": insertions,
            "deletions": deletions,
            "patch_bytes": len(patch_bytes),
            "sha256": hashlib.sha256(patch_bytes).hexdigest(),
            "files": files[:100],
            "files_truncated": len(files) > 100,
        }


@dataclass
class WorktreeSession:
    run_id: str
    repo_root: Path
    original_cwd: Path
    worktree_root: Path
    execution_cwd: Path
    base_commit: str
    artifact_dir: Path
    created_at: str
    removed: bool = False

    @classmethod
    def create(
        cls,
        start: Path,
        *,
        artifacts_dir: Path | None = None,
        run_id: str | None = None,
    ) -> "WorktreeSession":
        original_cwd = start.resolve()
        top_level = _run_git(original_cwd, "rev-parse", "--show-toplevel").stdout.strip()
        repo_root = Path(top_level).resolve()
        try:
            relative_cwd = original_cwd.relative_to(repo_root)
        except ValueError as exc:
            raise WorktreeError(f"working directory is outside Git repository: {original_cwd}") from exc

        status = _run_git(
            repo_root, "status", "--porcelain", "--untracked-files=all"
        ).stdout.strip()
        if status:
            raise WorktreeError(
                "Git worktree isolation requires a clean repository; commit or stash changes first:\n"
                f"{status}"
            )

        settings_path = _find_settings(original_cwd, repo_root)
        if settings_path is None:
            raise WorktreeError(
                "no .claude/settings.json was found inside the Git repository"
            )
        settings_relative = settings_path.relative_to(repo_root).as_posix()
        settings = _run_git(
            repo_root,
            "ls-files",
            "--error-unmatch",
            "--",
            settings_relative,
            check=False,
        )
        if settings.returncode != 0:
            raise WorktreeError(
                f"{settings_relative} must be tracked by Git before isolated AutoCI-Fix can run"
            )

        base_commit = _run_git(repo_root, "rev-parse", "HEAD").stdout.strip()
        run_id = run_id or generate_run_id()
        repo_key = hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest()[:12]
        worktree_base = Path(tempfile.gettempdir()).resolve() / "mini-claude-worktrees" / repo_key
        worktree_base.mkdir(parents=True, exist_ok=True)
        worktree_root = worktree_base / run_id

        artifact_base = (
            artifacts_dir.resolve()
            if artifacts_dir is not None
            else default_runs_base(repo_root)
        )
        artifact_dir = artifact_base / run_id
        artifact_dir.mkdir(parents=True, exist_ok=False)

        try:
            _run_git(
                repo_root,
                "worktree",
                "add",
                "--detach",
                str(worktree_root),
                base_commit,
            )
        except Exception:
            shutil.rmtree(artifact_dir, ignore_errors=True)
            raise

        return cls(
            run_id=run_id,
            repo_root=repo_root,
            original_cwd=original_cwd,
            worktree_root=worktree_root,
            execution_cwd=worktree_root / relative_cwd,
            base_commit=base_commit,
            artifact_dir=artifact_dir,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def snapshot(self) -> WorktreeSnapshot:
        status = _run_git(
            self.worktree_root, "status", "--short", "--untracked-files=all"
        ).stdout
        # Intent-to-add makes untracked files visible to `git diff HEAD` without
        # creating commits or changing the source checkout's index.
        _run_git(self.worktree_root, "add", "-N", "--", ".")
        patch = _run_git(
            self.worktree_root,
            "diff",
            "--binary",
            "--full-index",
            "HEAD",
        ).stdout
        numstat = _run_git(
            self.worktree_root,
            "diff",
            "--numstat",
            "--no-renames",
            "HEAD",
        ).stdout
        return WorktreeSnapshot(status=status, patch=patch, numstat=numstat)

    def write_artifacts(
        self,
        *,
        report: CiFixReport | None,
        isolation: dict,
        snapshot: WorktreeSnapshot,
        error: str | None = None,
    ) -> None:
        metadata = {
            **isolation,
            "created_at": self.created_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        write_run_artifacts(
            self.artifact_dir,
            report=report,
            metadata=metadata,
            status=snapshot.status,
            patch=snapshot.patch,
            error=error,
        )

    def cleanup(self) -> None:
        if self.removed:
            return
        _run_git(
            self.repo_root,
            "worktree",
            "remove",
            "--force",
            str(self.worktree_root),
        )
        _run_git(self.repo_root, "worktree", "prune")
        self.removed = True

    def isolation_metadata(self, *, succeeded: bool | None, preserved: bool) -> dict:
        return {
            "enabled": True,
            "run_id": self.run_id,
            "repo_root": str(self.repo_root),
            "original_cwd": str(self.original_cwd),
            "base_commit": self.base_commit,
            "worktree_root": str(self.worktree_root),
            "execution_cwd": str(self.execution_cwd),
            "artifact_dir": str(self.artifact_dir),
            "succeeded": succeeded,
            "worktree_preserved": preserved,
            "rolled_back": succeeded is not True and not preserved,
        }
