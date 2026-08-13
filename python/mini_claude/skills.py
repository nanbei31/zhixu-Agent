"""Discover, validate, and resolve project and user Skills."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


SKILL_METADATA_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "description"],
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        },
        "description": {"type": "string", "minLength": 1},
        "when-to-use": {"type": "string", "minLength": 1},
        "allowed-tools": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "user-invocable": {"type": "boolean"},
        "context": {"enum": ["inline", "fork"]},
    },
}

_METADATA_ALIASES = {
    "when_to_use": "when-to-use",
    "allowed_tools": "allowed-tools",
}


class SkillValidationError(ValueError):
    """Raised when a SKILL.md file is malformed or violates its schema."""


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    when_to_use: str | None = None
    allowed_tools: tuple[str, ...] | None = None
    user_invocable: bool = True
    context: str = "inline"
    prompt_template: str = ""
    source: str = "project"
    skill_dir: str = ""


_cached_skills: dict[tuple[Any, ...], tuple[SkillDefinition, ...]] = {}


def _find_project_skills_dir(start: Path) -> Path | None:
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".claude" / "skills"
        if candidate.is_dir():
            return candidate
    return None


def _directory_signature(path: Path | None) -> tuple[tuple[str, int, int], ...]:
    if path is None or not path.is_dir():
        return ()
    signature = []
    for skill_file in sorted(path.glob("*/SKILL.md")):
        try:
            stat = skill_file.stat()
        except OSError:
            continue
        signature.append((str(skill_file.resolve()), stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def discover_skills() -> list[SkillDefinition]:
    user_dir = Path.home() / ".claude" / "skills"
    project_dir = _find_project_skills_dir(Path.cwd())
    cache_key = (
        str(user_dir.resolve()),
        str(project_dir.resolve()) if project_dir else "",
        _directory_signature(user_dir),
        _directory_signature(project_dir),
    )
    cached = _cached_skills.get(cache_key)
    if cached is not None:
        return list(cached)

    skills: dict[str, SkillDefinition] = {}
    _load_skills_from_dir(user_dir, "user", skills)
    if project_dir is not None:
        _load_skills_from_dir(project_dir, "project", skills)

    discovered = tuple(skills.values())
    _cached_skills[cache_key] = discovered
    return list(discovered)


def _load_skills_from_dir(
    base_dir: Path, source: str, skills: dict[str, SkillDefinition]
) -> None:
    if not base_dir.is_dir():
        return
    for entry in sorted(base_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue
        skill = _parse_skill_file(skill_file, source, str(entry.resolve()))
        skills[skill.name] = skill


def _split_yaml_frontmatter(raw: str, file_path: Path) -> tuple[dict[str, Any], str]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillValidationError(f"{file_path}: YAML frontmatter must start with ---")

    end_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if end_index is None:
        raise SkillValidationError(f"{file_path}: YAML frontmatter is missing its closing ---")

    header = "\n".join(lines[1:end_index])
    try:
        loaded = yaml.safe_load(header)
    except yaml.YAMLError as exc:
        raise SkillValidationError(f"{file_path}: invalid YAML frontmatter: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise SkillValidationError(f"{file_path}: YAML frontmatter must be a mapping")

    body = "\n".join(lines[end_index + 1:]).strip()
    if not body:
        raise SkillValidationError(f"{file_path}: Skill prompt body must not be empty")
    return loaded, body


def _normalize_metadata(meta: dict[str, Any], file_path: Path) -> dict[str, Any]:
    normalized = dict(meta)
    for alias, canonical in _METADATA_ALIASES.items():
        if alias not in normalized:
            continue
        if canonical in normalized and normalized[canonical] != normalized[alias]:
            raise SkillValidationError(
                f"{file_path}: conflicting fields {canonical!r} and {alias!r}"
            )
        normalized[canonical] = normalized.pop(alias)

    def normalize_boolean(value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
        return value

    canonical_user_invocable = normalized.get("user-invocable")
    legacy_user_invocable = normalized.get("user_invocable")
    if canonical_user_invocable is not None and legacy_user_invocable is not None:
        if normalize_boolean(canonical_user_invocable) != normalize_boolean(legacy_user_invocable):
            raise SkillValidationError(
                f"{file_path}: conflicting fields 'user-invocable' and 'user_invocable'"
            )
    raw_user_invocable = normalized.get(
        "user-invocable",
        normalized.get("user_invocable", True),
    )
    normalized.pop("user_invocable", None)
    normalized["user-invocable"] = normalize_boolean(raw_user_invocable)

    normalized.setdefault("name", file_path.parent.name)
    normalized.setdefault("context", "inline")
    return normalized


def _validate_metadata(meta: dict[str, Any], file_path: Path) -> None:
    errors = sorted(
        Draft202012Validator(SKILL_METADATA_SCHEMA).iter_errors(meta),
        key=lambda error: list(error.absolute_path),
    )
    if not errors:
        return
    details = []
    for error in errors:
        field = ".".join(str(value) for value in error.absolute_path) or "frontmatter"
        details.append(f"{field}: {error.message}")
    raise SkillValidationError(f"{file_path}: invalid Skill metadata: {'; '.join(details)}")


def _parse_skill_file(file_path: Path, source: str, skill_dir: str) -> SkillDefinition:
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillValidationError(f"cannot read Skill file {file_path}: {exc}") from exc

    meta, body = _split_yaml_frontmatter(raw, file_path)
    meta = _normalize_metadata(meta, file_path)
    _validate_metadata(meta, file_path)
    allowed_tools = meta.get("allowed-tools")
    return SkillDefinition(
        name=meta["name"],
        description=meta["description"],
        when_to_use=meta.get("when-to-use"),
        allowed_tools=tuple(allowed_tools) if allowed_tools is not None else None,
        user_invocable=meta["user-invocable"],
        context=meta["context"],
        prompt_template=body,
        source=source,
        skill_dir=skill_dir,
    )


def get_skill_by_name(name: str) -> SkillDefinition | None:
    return next((skill for skill in discover_skills() if skill.name == name), None)


def resolve_skill_prompt(skill: SkillDefinition, args: str) -> str:
    prompt = re.sub(
        r"\$ARGUMENTS|\$\{ARGUMENTS\}",
        lambda _match: args,
        skill.prompt_template,
    )
    return prompt.replace("${CLAUDE_SKILL_DIR}", skill.skill_dir)


def execute_skill(skill_name: str, args: str) -> dict[str, Any] | None:
    skill = get_skill_by_name(skill_name)
    if not skill:
        return None
    return {
        "prompt": resolve_skill_prompt(skill, args),
        "allowed_tools": skill.allowed_tools,
        "context": skill.context,
    }


def build_skill_descriptions() -> str:
    skills = discover_skills()
    if not skills:
        return ""

    lines = ["# Available Skills", ""]
    invocable = [skill for skill in skills if skill.user_invocable]
    auto_only = [skill for skill in skills if not skill.user_invocable]

    if invocable:
        lines.append("User-invocable skills (user types /<name> to invoke):")
        for skill in invocable:
            lines.append(f"- **/{skill.name}**: {skill.description}")
            if skill.when_to_use:
                lines.append(f"  When to use: {skill.when_to_use}")
        lines.append("")

    if auto_only:
        lines.append("Auto-invocable skills (use the skill tool when appropriate):")
        for skill in auto_only:
            lines.append(f"- **{skill.name}**: {skill.description}")
            if skill.when_to_use:
                lines.append(f"  When to use: {skill.when_to_use}")
        lines.append("")

    lines.append(
        "To invoke a skill programmatically, use the `skill` tool with the skill name and optional arguments."
    )
    return "\n".join(lines)


def reset_skill_cache() -> None:
    _cached_skills.clear()
