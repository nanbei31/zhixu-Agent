"""Load BugsInPy's checked-out metadata without executing shell files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


@dataclass(frozen=True)
class BugsInPyCase:
    id: str
    project: str
    bug_id: int
    python_version: str
    buggy_commit: str
    fixed_commit: str
    test_files: tuple[str, ...]
    python_paths: tuple[str, ...]
    clone_url: str
    metadata_dir: Path
    requirements_file: Path
    test_script: Path
    setup_script: Path | None

    @property
    def title(self) -> str:
        return f"{self.project} bug {self.bug_id}"


@dataclass(frozen=True)
class BugsInPyCatalog:
    source: Path
    projects_dir: Path
    cases: tuple[BugsInPyCase, ...]

    def get(self, case_id: str) -> BugsInPyCase:
        normalized = case_id.strip().lower()
        for case in self.cases:
            if case.id.lower() == normalized:
                return case
        raise ValueError(f"unknown BugsInPy case: {case_id!r}")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_info_file(path: Path) -> dict[str, str]:
    """Parse the simple key=value format used by BugsInPy without sourcing it."""
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read BugsInPy metadata {path}: {exc}") from exc

    values: dict[str, str] = {}
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ASSIGNMENT_RE.fullmatch(line)
        if not match:
            raise ValueError(f"invalid BugsInPy metadata at {path}:{number}")
        values[match.group(1)] = _unquote(match.group(2))
    return values


def _split_paths(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.strip().replace("\\", "/")
            for item in value.split(";")
            if item.strip()
        )
    )


def _projects_dir(source: Path) -> Path:
    source = source.expanduser().resolve()
    nested = source / "projects"
    if nested.is_dir():
        return nested
    if source.is_dir() and any(source.glob("*/project.info")):
        return source
    raise ValueError(
        f"BugsInPy root must contain projects/*/project.info: {source}"
    )


def load_catalog(source: Path) -> BugsInPyCatalog:
    source = source.expanduser().resolve()
    projects_dir = _projects_dir(source)
    cases: list[BugsInPyCase] = []

    for project_dir in sorted(path for path in projects_dir.iterdir() if path.is_dir()):
        project_info_path = project_dir / "project.info"
        bugs_dir = project_dir / "bugs"
        if not project_info_path.is_file() or not bugs_dir.is_dir():
            continue
        project_info = parse_info_file(project_info_path)
        if project_info.get("status", "OK").upper() != "OK":
            continue
        clone_url = project_info.get("github_url", "").strip()
        if not clone_url:
            continue

        bug_dirs = sorted(
            (path for path in bugs_dir.iterdir() if path.is_dir() and path.name.isdigit()),
            key=lambda path: int(path.name),
        )
        for bug_dir in bug_dirs:
            bug_info_path = bug_dir / "bug.info"
            requirements_file = bug_dir / "requirements.txt"
            test_script = bug_dir / "run_test.sh"
            if not bug_info_path.is_file() or not test_script.is_file():
                continue
            bug_info = parse_info_file(bug_info_path)
            buggy_commit = bug_info.get("buggy_commit_id", "").strip()
            fixed_commit = bug_info.get("fixed_commit_id", "").strip()
            test_files = _split_paths(bug_info.get("test_file", ""))
            if not buggy_commit or not fixed_commit or not test_files:
                continue
            bug_id = int(bug_dir.name)
            cases.append(BugsInPyCase(
                id=f"{project_dir.name}-{bug_id}",
                project=project_dir.name,
                bug_id=bug_id,
                python_version=bug_info.get("python_version", "").strip(),
                buggy_commit=buggy_commit,
                fixed_commit=fixed_commit,
                test_files=test_files,
                python_paths=_split_paths(bug_info.get("pythonpath", "")),
                clone_url=clone_url,
                metadata_dir=bug_dir.resolve(),
                requirements_file=requirements_file.resolve(),
                test_script=test_script.resolve(),
                setup_script=(bug_dir / "setup.sh").resolve()
                if (bug_dir / "setup.sh").is_file()
                else None,
            ))

    if not cases:
        raise ValueError(f"no runnable BugsInPy cases found in {projects_dir}")
    return BugsInPyCatalog(
        source=source,
        projects_dir=projects_dir,
        cases=tuple(cases),
    )
