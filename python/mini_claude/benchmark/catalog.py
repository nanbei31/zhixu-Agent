"""Load and validate declarative benchmark suites."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator


SUITE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "version", "cases"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "version": {"type": "integer", "minimum": 1},
        "description": {"type": "string"},
        "cases": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/case"},
        },
    },
    "$defs": {
        "stringMap": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {"type": "string"},
        },
        "case": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "id", "title", "category", "difficulty", "files",
                "public_test", "hidden_test", "allowed_changes", "solution",
            ],
            "properties": {
                "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]+$"},
                "title": {"type": "string", "minLength": 1},
                "category": {"type": "string", "minLength": 1},
                "difficulty": {"enum": ["easy", "medium", "hard"]},
                "files": {"$ref": "#/$defs/stringMap"},
                "public_test": {"type": "string", "minLength": 1},
                "hidden_test": {"type": "string", "minLength": 1},
                "allowed_changes": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "solution": {"$ref": "#/$defs/stringMap"},
            },
        },
    },
}


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    title: str
    category: str
    difficulty: str
    files: dict[str, str]
    public_test: str
    hidden_test: str
    allowed_changes: tuple[str, ...]
    solution: dict[str, str]


@dataclass(frozen=True)
class BenchmarkSuite:
    name: str
    version: int
    description: str
    cases: tuple[BenchmarkCase, ...]
    source: Path


def default_suite_path() -> Path:
    return Path(__file__).parent / "suites" / "pytest-repair-40.yaml"


def load_suite(path: Path | None = None) -> BenchmarkSuite:
    source = (path or default_suite_path()).resolve()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load benchmark suite {source}: {exc}") from exc
    errors = sorted(Draft202012Validator(SUITE_SCHEMA).iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.path) or "<root>"
        raise ValueError(f"invalid benchmark suite at {location}: {error.message}")

    cases = tuple(
        BenchmarkCase(
            id=item["id"],
            title=item["title"],
            category=item["category"],
            difficulty=item["difficulty"],
            files=dict(item["files"]),
            public_test=item["public_test"],
            hidden_test=item["hidden_test"],
            allowed_changes=tuple(item["allowed_changes"]),
            solution=dict(item["solution"]),
        )
        for item in raw["cases"]
    )
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("benchmark case IDs must be unique")
    for case in cases:
        missing = set(case.allowed_changes) - set(case.files)
        if missing:
            raise ValueError(f"{case.id}: allowed_changes are not source files: {sorted(missing)}")
        unknown = set(case.solution) - set(case.files)
        if unknown:
            raise ValueError(f"{case.id}: solution contains unknown files: {sorted(unknown)}")
    return BenchmarkSuite(
        name=raw["name"],
        version=raw["version"],
        description=raw.get("description", ""),
        cases=cases,
        source=source,
    )


def materialize_case(case: BenchmarkCase, root: Path, *, solved: bool = False) -> None:
    files = dict(case.files)
    if solved:
        files.update(case.solution)
    for relative, content in files.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_public.py").write_text(case.public_test, encoding="utf-8")
    if solved:
        (tests / "test_hidden.py").write_text(case.hidden_test, encoding="utf-8")


def _pytest(root: Path, *tests: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *tests],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def validate_suite(suite: BenchmarkSuite) -> list[str]:
    """Return validation errors for initial failures and oracle solutions."""
    errors: list[str] = []
    for case in suite.cases:
        with tempfile.TemporaryDirectory(prefix=f"benchmark-broken-{case.id}-") as temp:
            root = Path(temp)
            materialize_case(case, root)
            initial = _pytest(root, "tests/test_public.py")
            if initial.returncode == 0:
                errors.append(f"{case.id}: public test unexpectedly passes before repair")
        with tempfile.TemporaryDirectory(prefix=f"benchmark-oracle-{case.id}-") as temp:
            root = Path(temp)
            materialize_case(case, root, solved=True)
            oracle = _pytest(root, "tests/test_public.py", "tests/test_hidden.py")
            if oracle.returncode != 0:
                detail = (oracle.stdout + oracle.stderr)[-1000:]
                errors.append(f"{case.id}: oracle solution fails\n{detail}")
    return errors
