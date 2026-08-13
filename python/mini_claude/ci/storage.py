"""Local run storage, JSONL events, and SQLite observability index."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import CiFixReport


def generate_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def get_data_root() -> Path:
    override = os.environ.get("MINI_CLAUDE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "MiniClaude"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MiniClaude"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "mini-claude"


def project_id(project_root: Path) -> str:
    normalized = str(project_root.resolve()).replace("\\", "/").lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def default_runs_base(project_root: Path) -> Path:
    return get_data_root() / "runs" / project_id(project_root)


@dataclass(frozen=True)
class RunPaths:
    run_id: str
    project_id: str
    run_dir: Path
    database_path: Path


def create_run_paths(
    project_root: Path,
    run_id: str,
    *,
    artifacts_dir: Path | None = None,
) -> RunPaths:
    pid = project_id(project_root)
    base = artifacts_dir.resolve() if artifacts_dir else default_runs_base(project_root)
    run_dir = base / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    database = get_database_path(artifacts_dir)
    database.parent.mkdir(parents=True, exist_ok=True)
    return RunPaths(run_id, pid, run_dir, database)


def get_database_path(artifacts_dir: Path | None = None) -> Path:
    if artifacts_dir is not None:
        return artifacts_dir.resolve() / "autoci.db"
    return get_data_root() / "autoci.db"


class EventRecorder:
    def __init__(self, run_id: str | None = None):
        self.run_id = run_id
        self._events: list[dict] = []
        self._path: Path | None = None

    def bind(self, run_dir: Path) -> Path:
        self._path = run_dir / "events.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in self._events),
            encoding="utf-8",
        )
        return self._path

    def emit(self, event_type: str, data: dict | None = None) -> None:
        event = {
            "sequence": len(self._events) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "type": event_type,
            "data": data or {},
        }
        self._events.append(event)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def __call__(self, event_type: str, data: dict) -> None:
        self.emit(event_type, data)


def write_run_artifacts(
    run_dir: Path,
    *,
    report: CiFixReport | None,
    metadata: dict,
    status: str = "",
    patch: str = "",
    error: str | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "git-status.txt").write_text(status, encoding="utf-8")
    (run_dir / "changes.patch").write_text(patch, encoding="utf-8")
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if report is not None:
        (run_dir / "report.json").write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        final = report.final_result
        final_log = final.stdout
        if final.stderr:
            final_log += ("\n" if final_log else "") + final.stderr
        (run_dir / "final-test.log").write_text(final_log, encoding="utf-8")
    if error:
        (run_dir / "error.txt").write_text(error + "\n", encoding="utf-8")


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    project_root TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL,
    model TEXT,
    provider TEXT,
    skill_name TEXT,
    skill_loaded INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_creation_tokens INTEGER NOT NULL,
    estimated_cost_usd REAL NOT NULL,
    total_duration_seconds REAL NOT NULL,
    agent_duration_seconds REAL NOT NULL,
    test_duration_seconds REAL NOT NULL,
    changed_file_count INTEGER NOT NULL,
    insertions INTEGER NOT NULL,
    deletions INTEGER NOT NULL,
    base_commit TEXT,
    artifact_dir TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_project_started
ON runs(project_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_model_skill
ON runs(model, skill_name);
"""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    database = path or (get_data_root() / "autoci.db")
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def index_run(
    report: CiFixReport,
    *,
    project_root: Path,
    artifact_dir: Path,
    database_path: Path | None = None,
) -> None:
    usage = report.usage or {}
    cost = report.cost or {}
    timing = report.timing or {}
    diff = report.diff or {}
    isolation = report.isolation or {}
    with closing(_connect(database_path)) as connection:
        with connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO runs (
                    run_id, project_id, project_root, started_at, finished_at,
                    status, model, provider, skill_name, skill_loaded, attempt_count,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_creation_tokens, estimated_cost_usd,
                    total_duration_seconds, agent_duration_seconds,
                    test_duration_seconds, changed_file_count, insertions, deletions,
                    base_commit, artifact_dir
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    report.run_id,
                    report.project_id,
                    str(project_root),
                    timing.get("started_at"),
                    timing.get("finished_at"),
                    "passed" if report.succeeded else "failed",
                    usage.get("model"),
                    usage.get("provider"),
                    report.skill_name,
                    int(report.skill_loaded),
                    len(report.attempts),
                    int(usage.get("input_tokens", 0)),
                    int(usage.get("output_tokens", 0)),
                    int(usage.get("cache_read_tokens", 0)),
                    int(usage.get("cache_creation_tokens", 0)),
                    float(cost.get("estimated_usd", 0.0)),
                    float(timing.get("total_duration_seconds", 0.0)),
                    float(timing.get("agent_duration_seconds", 0.0)),
                    float(timing.get("test_duration_seconds", 0.0)),
                    int(diff.get("changed_file_count", 0)),
                    int(diff.get("insertions", 0)),
                    int(diff.get("deletions", 0)),
                    isolation.get("base_commit"),
                    str(artifact_dir),
                ),
            )


def list_recent_runs(limit: int = 20) -> list[dict]:
    with closing(_connect()) as connection:
        rows = connection.execute(
            """
            SELECT run_id, started_at, status, model, skill_name, attempt_count,
                   input_tokens, output_tokens, estimated_cost_usd,
                   total_duration_seconds, changed_file_count, artifact_dir
            FROM runs ORDER BY started_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def usage_summary() -> dict:
    with closing(_connect()) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS run_count,
                   SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) AS passed_count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                   COALESCE(AVG(total_duration_seconds), 0) AS avg_duration_seconds,
                   COALESCE(AVG(attempt_count), 0) AS avg_attempts
            FROM runs
            """
        ).fetchone()
    return dict(row)
