"""Safe local workspace import, browsing, editing, and diff generation."""

from __future__ import annotations

import base64
import difflib
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..ci.storage import get_data_root
from ..workspace_policy import (
    BUILTIN_DENIED_COMPONENTS,
    BUILTIN_DENIED_NAMES,
    BUILTIN_DENIED_SUFFIXES,
    WorkspacePolicy,
)


MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_IMPORT_BYTES = 50 * 1024 * 1024
MAX_FILES = 3000
IGNORED_COMPONENTS = BUILTIN_DENIED_COMPONENTS | frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build"
})


@dataclass
class WebWorkspace:
    id: str
    root: Path
    name: str
    managed: bool
    baseline: dict[str, str] = field(default_factory=dict)
    checkpoints: list[dict[str, str]] = field(default_factory=list)
    excluded_paths: set[str] = field(default_factory=set)

    def policy(self, allow_shell: bool = False) -> WorkspacePolicy:
        tools = {"read_file", "list_files", "grep_search", "edit_file", "write_file"}
        effective_shell = allow_shell and not self.excluded_paths
        if effective_shell:
            tools.add("run_shell")
        return WorkspacePolicy(
            project_root=self.root,
            working_directory=self.root,
            readable_roots=(self.root,),
            writable_roots=(self.root,),
            deny_patterns=tuple(sorted(self.excluded_paths)),
            allowed_agent_tools=frozenset(tools),
            allow_agent_shell=effective_shell,
        )


class WorkspaceManager:
    def __init__(self, managed_root: Path | None = None):
        self.managed_root = (managed_root or get_data_root() / "web-workspaces").resolve()
        self.managed_root.mkdir(parents=True, exist_ok=True)
        self.workspaces: dict[str, WebWorkspace] = {}

    def open_path(self, raw_path: str) -> WebWorkspace:
        root = Path(raw_path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"目录不存在或不可读: {root}")
        workspace = WebWorkspace(uuid.uuid4().hex[:12], root, root.name, False)
        workspace.baseline = self._snapshot(root)
        self.workspaces[workspace.id] = workspace
        return workspace

    def import_files(self, name: str, files: list[dict]) -> WebWorkspace:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "workspace"
        workspace_id = uuid.uuid4().hex[:12]
        root = self.managed_root / f"{safe_name}-{workspace_id}"
        root.mkdir(parents=True)
        total = 0
        if not files or len(files) > MAX_FILES:
            raise ValueError(f"导入文件数必须在 1 到 {MAX_FILES} 之间")
        for item in files:
            relative = self._normalize_relative(item.get("path", ""))
            payload = base64.b64decode(item.get("content_base64", ""), validate=True)
            total += len(payload)
            if len(payload) > MAX_FILE_BYTES or total > MAX_IMPORT_BYTES:
                raise ValueError("导入内容超过大小限制")
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        workspace = WebWorkspace(workspace_id, root, safe_name, True)
        workspace.baseline = self._snapshot(root)
        self.workspaces[workspace.id] = workspace
        return workspace

    def get(self, workspace_id: str) -> WebWorkspace:
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError("工作区不存在或服务已重启")
        return workspace

    def tree(self, workspace_id: str) -> list[dict]:
        workspace = self.get(workspace_id)
        entries = []
        for path in self._iter_files(workspace.root):
            relative = path.relative_to(workspace.root).as_posix()
            if self._is_excluded(workspace, relative):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            entries.append({"path": relative, "name": path.name, "size": size})
        return entries

    def read_file(self, workspace_id: str, relative_path: str) -> dict:
        workspace = self.get(workspace_id)
        path = self._resolve(workspace, relative_path)
        decision = workspace.policy().check_path(path, write=False)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("文件过大，无法在编辑器中打开")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("暂不支持在编辑器中打开二进制文件") from exc
        return {"path": relative_path, "content": content, "size": path.stat().st_size}

    def write_file(self, workspace_id: str, relative_path: str, content: str) -> dict:
        workspace = self.get(workspace_id)
        path = self._resolve(workspace, relative_path)
        decision = workspace.policy().check_path(path, write=True)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ValueError("文件过大，无法保存")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        workspace.checkpoints.clear()
        return {"path": relative_path, "size": len(encoded)}

    def remove_access(self, workspace_id: str, relative_paths: list[str]) -> dict:
        workspace = self.get(workspace_id)
        if not relative_paths or len(relative_paths) > MAX_FILES:
            raise ValueError(f"移除文件数必须在 1 到 {MAX_FILES} 之间")
        removed: list[str] = []
        for relative_path in dict.fromkeys(relative_paths):
            path = self._resolve(workspace, relative_path)
            decision = workspace.policy().check_path(path, write=True)
            if not decision.allowed:
                raise PermissionError(decision.reason)
            if not path.is_file():
                raise FileNotFoundError(relative_path)
            relative = path.relative_to(workspace.root).as_posix()
            removed.append(relative)
        for relative in removed:
            workspace.excluded_paths.add(relative)
            workspace.baseline.pop(relative, None)
        workspace.checkpoints.clear()
        return {
            "removed": len(removed),
            "paths": removed,
        }

    def clear_access(self, workspace_id: str, confirmation: str) -> dict:
        workspace = self.get(workspace_id)
        if confirmation.strip() != workspace.name:
            raise ValueError("工作区名称确认不匹配")
        paths = [
            path.relative_to(workspace.root).as_posix()
            for path in self._iter_files(workspace.root)
            if not self._is_excluded(
                workspace, path.relative_to(workspace.root).as_posix()
            )
        ]
        if not paths:
            return {"removed": 0, "paths": []}
        return self.remove_access(workspace_id, paths)

    def create_checkpoint(self, workspace_id: str) -> None:
        workspace = self.get(workspace_id)
        workspace.checkpoints.append(self._snapshot(workspace.root, workspace.excluded_paths))
        if len(workspace.checkpoints) > 10:
            workspace.checkpoints.pop(0)

    def finalize_checkpoint(self, workspace_id: str) -> bool:
        workspace = self.get(workspace_id)
        if not workspace.checkpoints:
            return False
        if workspace.checkpoints[-1] == self._snapshot(workspace.root, workspace.excluded_paths):
            workspace.checkpoints.pop()
            return False
        return True

    def undo_last_change(self, workspace_id: str) -> dict:
        workspace = self.get(workspace_id)
        if not workspace.checkpoints:
            raise ValueError("没有可撤销的 Agent 修改")
        target = workspace.checkpoints.pop()
        current = self._snapshot(workspace.root, workspace.excluded_paths)
        restored = []
        removed = []

        for relative, content in target.items():
            if current.get(relative) == content:
                continue
            path = self._resolve(workspace, relative)
            decision = workspace.policy().check_path(path, write=True)
            if not decision.allowed:
                raise PermissionError(decision.reason)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            restored.append(relative)

        for relative in sorted(set(current) - set(target)):
            path = self._resolve(workspace, relative)
            decision = workspace.policy().check_path(path, write=True)
            if not decision.allowed:
                raise PermissionError(decision.reason)
            path.unlink()
            self._remove_empty_parents(path.parent, workspace.root)
            removed.append(relative)

        return {
            "restored": restored,
            "removed": removed,
            "changed_files": len(restored) + len(removed),
            "can_undo": bool(workspace.checkpoints),
        }

    def diff(self, workspace_id: str) -> dict:
        workspace = self.get(workspace_id)
        current = self._snapshot(workspace.root, workspace.excluded_paths)
        files = []
        chunks = []
        for relative in sorted(set(workspace.baseline) | set(current)):
            before = workspace.baseline.get(relative, "")
            after = current.get(relative, "")
            if before == after:
                continue
            status = "modified"
            if relative not in workspace.baseline:
                status = "added"
            elif relative not in current:
                status = "deleted"
            files.append({"path": relative, "status": status})
            chunks.extend(difflib.unified_diff(
                before.splitlines(), after.splitlines(),
                fromfile=f"a/{relative}", tofile=f"b/{relative}", lineterm="",
            ))
        return {
            "files": files,
            "patch": "\n".join(chunks),
            "can_undo": bool(workspace.checkpoints),
        }

    def _resolve(self, workspace: WebWorkspace, relative_path: str) -> Path:
        relative = self._normalize_relative(relative_path)
        path = (workspace.root / relative).resolve(strict=False)
        try:
            path.relative_to(workspace.root)
        except ValueError as exc:
            raise PermissionError("路径超出工作区") from exc
        return path

    @staticmethod
    def _normalize_relative(raw: str) -> Path:
        value = raw.replace("\\", "/").lstrip("/")
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError("无效的工作区相对路径")
        if any(part in IGNORED_COMPONENTS for part in path.parts):
            raise PermissionError("路径位于受保护目录")
        if _is_sensitive(path):
            raise PermissionError("路径是受保护的凭据或环境文件")
        return path

    def _snapshot(
        self,
        root: Path,
        excluded_paths: set[str] | None = None,
    ) -> dict[str, str]:
        snapshot = {}
        for path in self._iter_files(root):
            relative = path.relative_to(root).as_posix()
            if self._path_is_excluded(relative, excluded_paths or set()):
                continue
            try:
                if path.stat().st_size <= MAX_FILE_BYTES:
                    snapshot[relative] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        return snapshot

    @classmethod
    def _is_excluded(cls, workspace: WebWorkspace, relative: str) -> bool:
        return cls._path_is_excluded(relative, workspace.excluded_paths)

    @staticmethod
    def _path_is_excluded(relative: str, excluded_paths: set[str]) -> bool:
        return any(
            relative == excluded or relative.startswith(excluded.rstrip("/") + "/")
            for excluded in excluded_paths
        )

    @staticmethod
    def _remove_empty_parents(directory: Path, root: Path) -> None:
        current = directory
        while current != root:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    @staticmethod
    def _iter_files(root: Path):
        count = 0
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name for name in dirnames
                if (
                    name not in IGNORED_COMPONENTS
                    and not name.endswith(".egg-info")
                    and not (Path(directory) / name).is_symlink()
                )
            )
            for filename in sorted(filenames):
                path = Path(directory) / filename
                if path.is_symlink():
                    continue
                relative = path.relative_to(root)
                if (
                    any(part in IGNORED_COMPONENTS for part in relative.parts)
                    or _is_sensitive(relative)
                ):
                    continue
                count += 1
                if count > MAX_FILES:
                    return
                yield path


def _is_sensitive(path: Path) -> bool:
    name = path.name
    return (
        name in BUILTIN_DENIED_NAMES
        or (name.startswith(".env.") and name != ".env.example")
        or path.suffix.lower() in BUILTIN_DENIED_SUFFIXES
    )
