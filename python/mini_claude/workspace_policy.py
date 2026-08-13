"""Persistent workspace boundaries for agent file and shell tools."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path


PATH_TOOLS = {"read_file", "write_file", "edit_file", "list_files", "grep_search"}
WRITE_TOOLS = {"write_file", "edit_file"}

BUILTIN_DENIED_COMPONENTS = frozenset({
    ".git",
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
    ".venv",
    "node_modules",
})
BUILTIN_DENIED_NAMES = frozenset({
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
})
BUILTIN_DENIED_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})


class WorkspacePolicyError(ValueError):
    """Raised when a project policy is missing or invalid."""


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""
    resolved_path: Path | None = None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_under(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve(strict=False)


@dataclass(frozen=True)
class WorkspacePolicy:
    project_root: Path
    working_directory: Path
    readable_roots: tuple[Path, ...]
    writable_roots: tuple[Path, ...]
    deny_patterns: tuple[str, ...]
    allowed_agent_tools: frozenset[str]
    allow_agent_shell: bool = False
    targets: tuple[Path, ...] = ()

    def _relative_path(self, path: Path) -> str | None:
        try:
            return path.relative_to(self.project_root).as_posix()
        except ValueError:
            return None

    def _sensitive_reason(self, path: Path) -> str | None:
        relative = self._relative_path(path)
        if relative is None:
            return "path is outside the configured project root"

        parts = path.relative_to(self.project_root).parts
        if any(part in BUILTIN_DENIED_COMPONENTS for part in parts):
            return "path contains a protected directory"

        name = path.name
        if name in BUILTIN_DENIED_NAMES:
            return "path is a protected credential or environment file"
        if name.startswith(".env.") and name != ".env.example":
            return "path is a protected environment file"
        if path.suffix.lower() in BUILTIN_DENIED_SUFFIXES:
            return "path has a protected credential suffix"

        for pattern in self.deny_patterns:
            normalized = pattern.replace("\\", "/").rstrip("/")
            if not normalized:
                continue
            if fnmatch.fnmatch(relative, normalized) or relative == normalized:
                return f"path matches denied pattern: {pattern}"
            if relative.startswith(normalized + "/"):
                return f"path is under denied path: {pattern}"
        return None

    def check_path(self, value: str | Path, *, write: bool) -> PolicyDecision:
        path = _resolve_under(self.working_directory, value)
        sensitive_reason = self._sensitive_reason(path)
        if sensitive_reason:
            return PolicyDecision(False, sensitive_reason, path)

        roots = self.writable_roots if write else self.readable_roots
        if not any(_is_within(path, root) for root in roots):
            boundary = "writable" if write else "readable"
            return PolicyDecision(False, f"path is outside configured {boundary} roots", path)
        return PolicyDecision(True, resolved_path=path)

    def check_tool_call(self, tool_name: str, inp: dict) -> PolicyDecision:
        if tool_name == "run_shell" and not self.allow_agent_shell:
            return PolicyDecision(False, "agent shell is disabled by workspace policy")
        if tool_name not in self.allowed_agent_tools:
            return PolicyDecision(False, f"tool is not allowed by workspace policy: {tool_name}")
        if tool_name not in PATH_TOOLS:
            return PolicyDecision(True)

        key = "file_path" if "file_path" in inp else "path"
        value = inp.get(key) or "."
        return self.check_path(value, write=tool_name in WRITE_TOOLS)

    def relative_targets(self) -> tuple[str, ...]:
        return tuple(path.relative_to(self.project_root).as_posix() for path in self.targets)

    def relative_writable_roots(self) -> tuple[str, ...]:
        values = []
        for root in self.writable_roots:
            relative = root.relative_to(self.project_root).as_posix()
            values.append(relative or ".")
        return tuple(values)

    def to_dict(self) -> dict:
        return {
            "project_root": str(self.project_root),
            "working_directory": str(self.working_directory),
            "readable_paths": [str(path) for path in self.readable_roots],
            "writable_paths": [str(path) for path in self.writable_roots],
            "targets": list(self.relative_targets()),
            "allowed_agent_tools": sorted(self.allowed_agent_tools),
            "allow_agent_shell": self.allow_agent_shell,
        }


def _find_project_settings(start: Path) -> Path | None:
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".claude" / "settings.json"
        if candidate.is_file():
            return candidate
    return None


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspacePolicyError(f"cannot load workspace policy from {path}: {exc}") from exc


def _resolve_project_root(settings_path: Path, raw: dict) -> Path:
    """Resolve projectRoot relative to the checked-in settings file.

    The directory containing .claude/ is the trust anchor. An explicit
    projectRoot remains supported for compatibility, but it must resolve to
    that same directory so a policy cannot silently widen its own boundary.
    """
    settings_root = settings_path.parent.parent.resolve()
    configured = raw.get("projectRoot", ".")
    if not isinstance(configured, str) or not configured.strip():
        raise WorkspacePolicyError("workspacePolicy.projectRoot must be a path string")

    configured_path = Path(configured).expanduser()
    project_root = (
        configured_path.resolve(strict=False)
        if configured_path.is_absolute()
        else (settings_root / configured_path).resolve(strict=False)
    )
    if project_root != settings_root:
        raise WorkspacePolicyError(
            "workspacePolicy.projectRoot must resolve to the directory containing .claude: "
            f"expected {settings_root}, got {project_root}"
        )
    return project_root


def _configured_roots(root: Path, values: object, field: str) -> tuple[Path, ...]:
    if not isinstance(values, list) or not values:
        raise WorkspacePolicyError(f"workspacePolicy.{field} must be a non-empty list")
    roots = tuple(_resolve_under(root, str(value)) for value in values)
    for resolved in roots:
        if not _is_within(resolved, root):
            raise WorkspacePolicyError(f"workspacePolicy.{field} escapes project root: {resolved}")
    return roots


def load_workspace_policy(
    start: Path,
    *,
    cli_allowed_paths: tuple[str, ...] = (),
    targets: tuple[str, ...] = (),
) -> WorkspacePolicy:
    settings_path = _find_project_settings(start)
    if not settings_path:
        raise WorkspacePolicyError(
            "no .claude/settings.json was found; AutoCI-Fix requires a persistent workspacePolicy"
        )

    settings = _load_json(settings_path)
    raw = settings.get("workspacePolicy")
    if not isinstance(raw, dict):
        raise WorkspacePolicyError(f"workspacePolicy is missing from {settings_path}")

    project_root = _resolve_project_root(settings_path, raw)

    start_resolved = start.resolve()
    if not _is_within(start_resolved, project_root):
        raise WorkspacePolicyError(
            f"current directory {start_resolved} is outside configured projectRoot {project_root}"
        )

    readable_roots = _configured_roots(
        project_root, raw.get("readablePaths", ["."]), "readablePaths"
    )
    configured_writable = _configured_roots(
        project_root, raw.get("writablePaths"), "writablePaths"
    )

    if cli_allowed_paths:
        narrowed = tuple(_resolve_under(start_resolved, value) for value in cli_allowed_paths)
        for path in narrowed:
            if not any(_is_within(path, root) for root in configured_writable):
                raise WorkspacePolicyError(
                    f"CLI allowed path cannot widen project policy: {path}"
                )
        writable_roots = narrowed
    else:
        writable_roots = configured_writable

    resolved_targets = tuple(_resolve_under(start_resolved, value) for value in targets)
    for target in resolved_targets:
        if not _is_within(target, project_root):
            raise WorkspacePolicyError(f"target escapes project root: {target}")
        if not target.exists():
            raise WorkspacePolicyError(f"target does not exist: {target}")

    raw_tools = raw.get("agentTools", [
        "read_file", "list_files", "grep_search", "edit_file", "write_file"
    ])
    if not isinstance(raw_tools, list):
        raise WorkspacePolicyError("workspacePolicy.agentTools must be a list")

    deny_patterns = raw.get("denyPaths", [])
    if not isinstance(deny_patterns, list):
        raise WorkspacePolicyError("workspacePolicy.denyPaths must be a list")

    policy = WorkspacePolicy(
        project_root=project_root,
        working_directory=start_resolved,
        readable_roots=readable_roots,
        writable_roots=writable_roots,
        deny_patterns=tuple(str(value) for value in deny_patterns),
        allowed_agent_tools=frozenset(str(value) for value in raw_tools),
        allow_agent_shell=bool(raw.get("allowAgentShell", False)),
        targets=resolved_targets,
    )
    for target in resolved_targets:
        decision = policy.check_path(target, write=False)
        if not decision.allowed:
            raise WorkspacePolicyError(
                f"target is blocked by workspace policy: {target}: {decision.reason}"
            )
    return policy
