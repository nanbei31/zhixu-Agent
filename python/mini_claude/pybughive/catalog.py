"""Load PyBugHive's offline JSON metadata without MongoDB."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PyBugHiveCase:
    id: str
    username: str
    repository: str
    issue_id: int
    title: str
    fix_commit: str
    buggy_commit: str
    test_files: tuple[str, ...]
    target_files: tuple[str, ...]
    install_steps: tuple[str, ...]
    test_steps: tuple[str, ...]
    full_test_steps: tuple[str, ...]
    clone_url: str


@dataclass(frozen=True)
class PyBugHiveCatalog:
    source: Path
    cases: tuple[PyBugHiveCase, ...]

    def get(self, case_id: str) -> PyBugHiveCase:
        normalized = case_id.strip().lower()
        for case in self.cases:
            if case.id.lower() == normalized:
                return case
        raise ValueError(f"unknown PyBugHive case: {case_id!r}")


def _steps(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(line.strip() for line in value.splitlines() if line.strip())


def _is_source_target(path: str, test_files: set[str]) -> bool:
    normalized = path.replace("\\", "/")
    lowered = normalized.lower()
    if normalized in test_files:
        return False
    if lowered.startswith(("doc/", "docs/", "changelog", "changes")):
        return False
    if "/test/" in lowered or "/tests/" in lowered or lowered.startswith(("test/", "tests/")):
        return False
    if Path(normalized).name.lower() in {
        "setup.py",
        "setup.cfg",
        "pyproject.toml",
        "pipfile",
        "pipfile.lock",
        "poetry.lock",
    }:
        return False
    return Path(normalized).suffix.lower() in {".py", ".pyi", ".pyx", ".pxd"}


def load_catalog(source: Path) -> PyBugHiveCatalog:
    source = source.expanduser().resolve()
    try:
        projects = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load PyBugHive dataset {source}: {exc}") from exc
    if not isinstance(projects, list):
        raise ValueError("PyBugHive dataset root must be a list of projects")

    cases: list[PyBugHiveCase] = []
    for project in projects:
        if not isinstance(project, dict):
            continue
        username = str(project.get("username") or "").strip()
        repository = str(project.get("repository") or "").strip()
        project_install = _steps(project.get("installSteps"))
        for issue in project.get("issues") or []:
            if not isinstance(issue, dict) or not issue.get("commits"):
                continue
            commit = issue["commits"][0]
            stat = commit.get("stat") or {}
            issue_id = int(issue["id"])
            test_files = tuple(
                str(item["filename"]).replace("\\", "/")
                for item in stat.get("tests") or []
                if isinstance(item, dict)
                and item.get("filename")
                and item.get("status") != "removed"
            )
            test_file_set = set(test_files)
            target_files = tuple(dict.fromkeys(
                str(item["filename"]).replace("\\", "/")
                for item in stat.get("files") or []
                if isinstance(item, dict)
                and item.get("filename")
                and item.get("status") != "removed"
                and _is_source_target(str(item["filename"]), test_file_set)
            ))
            parents = str(commit.get("parents") or "").split()
            if not username or not repository or not parents or not commit.get("hash"):
                continue
            cases.append(PyBugHiveCase(
                id=f"{repository}-{issue_id}",
                username=username,
                repository=repository,
                issue_id=issue_id,
                title=str(issue.get("title") or ""),
                fix_commit=str(commit["hash"]),
                buggy_commit=parents[0],
                test_files=test_files,
                target_files=target_files,
                install_steps=_steps(issue.get("installSteps")) or project_install,
                test_steps=_steps(issue.get("testSteps")),
                full_test_steps=_steps(issue.get("testStepsFull")),
                clone_url=f"https://github.com/{username}/{repository}.git",
            ))
    if not cases:
        raise ValueError(f"no runnable PyBugHive cases found in {source}")
    return PyBugHiveCatalog(source=source, cases=tuple(cases))
