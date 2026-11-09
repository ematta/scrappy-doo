"""Local storage for execution records, reviews, and artifacts."""

import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator
from uuid import uuid4

from scrappy_doo.execution import (
    CleanupStatus,
    Execution,
    ExecutionStatus,
    Finding,
    FindingResult,
)


class ExecutionStore:
    """Persist executions under a caller-selected local data directory."""

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._database = self.data_dir / "executions.sqlite3"
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY,
                    application TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY,
                    execution_id TEXT NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
                    entry TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge (
                    id TEXT PRIMARY KEY,
                    application TEXT NOT NULL,
                    environment TEXT,
                    text TEXT NOT NULL,
                    source_execution_id TEXT NOT NULL,
                    source_deleted INTEGER NOT NULL DEFAULT 0,
                    evidence TEXT NOT NULL,
                    conflicts_with TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _serialize(execution: Execution) -> str:
        return json.dumps(
            {
                "goal_verified": execution.goal_verified,
                "status": execution.status.value,
                "cleanup_status": execution.cleanup_status.value,
                "findings": [
                    {
                        "expected": finding.expected,
                        "observed": finding.observed,
                        "result": finding.original_result.value,
                        "evidence": list(finding.evidence),
                        "reason": finding.reason,
                        "reviews": [
                            {"result": review.result.value, "reason": review.reason}
                            for review in finding.reviews
                        ],
                    }
                    for finding in execution.findings
                ],
            }
        )

    @staticmethod
    def _deserialize(data: str) -> Execution:
        record = json.loads(data)
        findings = []
        for item in record["findings"]:
            finding = Finding(
                expected=item["expected"],
                observed=item["observed"],
                result=FindingResult(item["result"]),
                evidence=tuple(item["evidence"]),
                reason=item["reason"],
            )
            for review in item["reviews"]:
                finding.review(FindingResult(review["result"]), reason=review["reason"])
            findings.append(finding)
        return Execution(
            findings=tuple(findings),
            goal_verified=record["goal_verified"],
            status=ExecutionStatus(record["status"]),
            cleanup_status=CleanupStatus(record["cleanup_status"]),
        )

    @staticmethod
    def _load(connection: sqlite3.Connection, execution_id: str) -> Execution:
        row = connection.execute(
            "SELECT data FROM executions WHERE id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            raise KeyError(execution_id)
        return ExecutionStore._deserialize(row[0])

    def save_execution(self, *, application: str, environment: str, execution: Execution) -> str:
        execution_id = uuid4().hex
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO executions (id, application, environment, data) VALUES (?, ?, ?, ?)",
                (execution_id, application, environment, self._serialize(execution)),
            )
        return execution_id

    def get_execution(self, execution_id: str) -> Execution:
        with self._connect() as connection:
            return self._load(connection, execution_id)

    def list_executions(self, *, application: str, environment: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM executions WHERE application = ? AND environment = ? ORDER BY rowid",
                (application, environment),
            ).fetchall()
        return [row[0] for row in rows]

    def review_finding(
        self, execution_id: str, finding_index: int, result: FindingResult, *, reason: str
    ) -> None:
        with self._connect() as connection:
            # Acquire the write lock before reading so concurrent reviews are not lost.
            connection.execute("BEGIN IMMEDIATE")
            execution = self._load(connection, execution_id)
            if finding_index < 0 or finding_index >= len(execution.findings):
                raise IndexError(finding_index)
            execution.findings[finding_index].review(result, reason=reason)
            connection.execute(
                "UPDATE executions SET data = ? WHERE id = ?",
                (self._serialize(execution), execution_id),
            )

    def _execution_dir(self, execution_id: str) -> Path:
        # Look up the identifier before using it in a path.
        self.get_execution(execution_id)
        return self.data_dir / "executions" / execution_id

    def _artifact_path(self, execution_id: str, name: str) -> Path:
        root = (self._execution_dir(execution_id) / "artifacts").resolve()
        relative = Path(name)
        if not name or relative.is_absolute() or ".." in relative.parts or relative == Path("."):
            raise ValueError("Artifact name must be a relative path within the execution")
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Artifact path escapes the execution")
        return path

    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
                temporary_name = temporary.name
                temporary.write(data)
            os.replace(temporary_name, path)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def save_artifact(self, execution_id: str, name: str, data: bytes) -> None:
        self._write_file(self._artifact_path(execution_id, name), data)

    def read_artifact(self, execution_id: str, name: str) -> bytes:
        return self._artifact_path(execution_id, name).read_bytes()

    def save_report(self, execution_id: str, report: str) -> None:
        self._write_file(self._execution_dir(execution_id) / "report.txt", report.encode("utf-8"))

    def read_report(self, execution_id: str) -> str:
        return (self._execution_dir(execution_id) / "report.txt").read_text(encoding="utf-8")

    def append_history(self, execution_id: str, entry: str) -> None:
        with self._connect() as connection:
            self._load(connection, execution_id)
            connection.execute(
                "INSERT INTO history (execution_id, entry) VALUES (?, ?)", (execution_id, entry)
            )

    def read_history(self, execution_id: str) -> list[str]:
        with self._connect() as connection:
            self._load(connection, execution_id)
            rows = connection.execute(
                "SELECT entry FROM history WHERE execution_id = ? ORDER BY id", (execution_id,)
            ).fetchall()
        return [row[0] for row in rows]

    def delete_execution(self, execution_id: str) -> None:
        directory = self._execution_dir(execution_id)
        if directory.exists():
            shutil.rmtree(directory)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM knowledge WHERE source_execution_id = ? AND status IN ('proposed', 'rejected')",
                (execution_id,),
            )
            connection.execute(
                "UPDATE knowledge SET source_deleted = 1 WHERE source_execution_id = ?",
                (execution_id,),
            )
            connection.execute("DELETE FROM executions WHERE id = ?", (execution_id,))
