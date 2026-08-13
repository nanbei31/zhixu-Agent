"""Structured results shared by GitHub Actions helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class PatchValidationResult:
    valid: bool
    base_commit: str
    patch_sha256: str
    patch_bytes: int
    changed_files: list[str] = field(default_factory=list)
    added_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"schema_version": 1, **asdict(self)}
