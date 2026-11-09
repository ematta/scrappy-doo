"""Reviewable application knowledge associated with local executions."""

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from scrappy_doo.storage import ExecutionStore


@dataclass(frozen=True)
class Knowledge:
    id: str
    application: str
    environment: str | None
    text: str
    source_execution_id: str
    source_deleted: bool
    evidence: tuple[str, ...]
    conflicts_with: tuple[str, ...]
    status: str


class KnowledgeStore:
    """Store knowledge proposals and human review decisions locally."""

    def __init__(self, data_dir: Path | str):
        self._executions = ExecutionStore(data_dir)

    def propose(
        self,
        *,
        application: str,
        environment: str | None,
        text: str,
        source_execution_id: str,
        evidence: tuple[str, ...],
        conflicts_with: tuple[str, ...] = (),
    ) -> str:
        if not text.strip():
            raise ValueError("Knowledge text is required")
        knowledge_id = uuid4().hex
        with self._executions._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source = connection.execute(
                "SELECT application, environment FROM executions WHERE id = ?",
                (source_execution_id,),
            ).fetchone()
            if source is None:
                raise KeyError(source_execution_id)
            if source[0] != application or (environment is not None and source[1] != environment):
                raise ValueError("Source execution must match the knowledge scope")
            for conflict_id in conflicts_with:
                conflict = connection.execute(
                    "SELECT application, environment, status FROM knowledge WHERE id = ?",
                    (conflict_id,),
                ).fetchone()
                if conflict is None:
                    raise KeyError(conflict_id)
                if conflict[0] != application or conflict[2] != "approved":
                    raise ValueError("Conflicts must refer to approved knowledge of the same application")
            connection.execute(
                """INSERT INTO knowledge
                   (id, application, environment, text, source_execution_id, evidence, conflicts_with, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed')""",
                (knowledge_id, application, environment, text, source_execution_id,
                 json.dumps(evidence), json.dumps(conflicts_with)),
            )
        return knowledge_id

    def get(self, knowledge_id: str) -> Knowledge:
        with self._executions._connect() as connection:
            row = connection.execute(
                """SELECT id, application, environment, text, source_execution_id,
                          source_deleted, evidence, conflicts_with, status
                   FROM knowledge WHERE id = ?""",
                (knowledge_id,),
            ).fetchone()
        if row is None:
            raise KeyError(knowledge_id)
        return Knowledge(
            id=row[0], application=row[1], environment=row[2], text=row[3],
            source_execution_id=row[4], source_deleted=bool(row[5]),
            evidence=tuple(json.loads(row[6])), conflicts_with=tuple(json.loads(row[7])),
            status=row[8],
        )

    def list_proposals(self, *, application: str) -> list[str]:
        with self._executions._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM knowledge WHERE application = ? AND status = 'proposed' ORDER BY rowid",
                (application,),
            ).fetchall()
        return [row[0] for row in rows]

    def list_approved(self, *, application: str, environment: str) -> list[str]:
        with self._executions._connect() as connection:
            rows = connection.execute(
                """SELECT id FROM knowledge WHERE application = ? AND status = 'approved'
                   AND (environment IS NULL OR environment = ?) ORDER BY rowid""",
                (application, environment),
            ).fetchall()
        return [row[0] for row in rows]

    def _transition(self, knowledge_id: str, *, expected: str, target: str) -> None:
        with self._executions._connect() as connection:
            cursor = connection.execute(
                "UPDATE knowledge SET status = ? WHERE id = ? AND status = ?",
                (target, knowledge_id, expected),
            )
            if cursor.rowcount == 0:
                exists = connection.execute("SELECT 1 FROM knowledge WHERE id = ?", (knowledge_id,)).fetchone()
                if exists is None:
                    raise KeyError(knowledge_id)
                raise ValueError(f"Knowledge must be {expected} to become {target}")

    def approve(self, knowledge_id: str) -> None:
        self._transition(knowledge_id, expected="proposed", target="approved")

    def reject(self, knowledge_id: str) -> None:
        self._transition(knowledge_id, expected="proposed", target="rejected")

    def retire(self, knowledge_id: str) -> None:
        self._transition(knowledge_id, expected="approved", target="retired")

    def edit(self, knowledge_id: str, *, text: str) -> None:
        if not text.strip():
            raise ValueError("Knowledge text is required")
        with self._executions._connect() as connection:
            cursor = connection.execute(
                "UPDATE knowledge SET text = ? WHERE id = ? AND status IN ('proposed', 'approved')",
                (text, knowledge_id),
            )
            if cursor.rowcount == 0:
                exists = connection.execute("SELECT 1 FROM knowledge WHERE id = ?", (knowledge_id,)).fetchone()
                if exists is None:
                    raise KeyError(knowledge_id)
                raise ValueError("Only proposed or approved knowledge can be edited")
