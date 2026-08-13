"""JSON report persistence for AutoCI-Fix."""

from __future__ import annotations

import json
from pathlib import Path

from .models import CiFixReport


def write_json_report(report: CiFixReport, path: Path) -> Path:
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return resolved
