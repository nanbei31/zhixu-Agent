"""Best-effort parser for pytest terminal output."""

from __future__ import annotations

import re

from .models import PytestFailure, PytestSummary


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_LOCATION_RE = re.compile(
    r"^(?P<path>(?:[A-Za-z]:)?[^:\r\n]+?\.py):(?P<line>\d+):\s*(?P<detail>.*)$"
)
_COUNT_RE = re.compile(
    r"(?P<count>\d+)\s+"
    r"(?P<label>failed|passed|error|errors|skipped|xfailed|xpassed)\b"
)
_DURATION_RE = re.compile(r"\bin\s+(?P<seconds>\d+(?:\.\d+)?)s\b")


def _split_failed_line(line: str) -> tuple[str, str | None]:
    rest = line[len("FAILED "):].strip()
    if " - " in rest:
        node_id, message = rest.split(" - ", 1)
        return node_id.strip(), message.strip() or None
    return rest, None


def _test_name(node_id: str) -> str | None:
    parts = node_id.split("::")
    return parts[-1] if len(parts) > 1 else None


def parse_pytest_output(output: str) -> PytestSummary:
    """Extract failure nodes, source locations, and summary counts."""
    clean = _ANSI_RE.sub("", output.replace("\r\n", "\n"))
    failures: list[PytestFailure] = []
    locations: list[str] = []
    error_messages: list[str] = []
    counts: dict[str, int] = {}
    duration: float | None = None

    for raw_line in clean.splitlines():
        line = raw_line.strip()
        if line.startswith("FAILED "):
            node_id, message = _split_failed_line(line)
            file_path = node_id.split("::", 1)[0] or None
            failures.append(
                PytestFailure(
                    node_id=node_id,
                    file_path=file_path,
                    test_name=_test_name(node_id),
                    message=message,
                )
            )

        location_match = _LOCATION_RE.match(line)
        if location_match:
            location = f"{location_match.group('path')}:{location_match.group('line')}"
            if location not in locations:
                locations.append(location)

        if line.startswith("E "):
            message = line[1:].strip()
            if message and message not in error_messages:
                error_messages.append(message)

        if _DURATION_RE.search(line):
            line_counts = {
                match.group("label"): int(match.group("count"))
                for match in _COUNT_RE.finditer(line)
            }
            if line_counts:
                counts = line_counts
                duration_match = _DURATION_RE.search(line)
                if duration_match:
                    duration = float(duration_match.group("seconds"))

    if error_messages:
        enriched = []
        for index, failure in enumerate(failures):
            message = failure.message
            if not message and index < len(error_messages):
                message = error_messages[index]
            enriched.append(
                PytestFailure(
                    node_id=failure.node_id,
                    file_path=failure.file_path,
                    test_name=failure.test_name,
                    message=message,
                )
            )
        failures = enriched

    return PytestSummary(
        failed=counts.get("failed", 0),
        passed=counts.get("passed", 0),
        errors=counts.get("error", counts.get("errors", 0)),
        skipped=counts.get("skipped", 0),
        xfailed=counts.get("xfailed", 0),
        xpassed=counts.get("xpassed", 0),
        duration_seconds=duration,
        failures=tuple(failures),
        locations=tuple(locations),
    )
