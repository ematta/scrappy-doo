"""Durable execution-boundary events for controlled evaluation."""

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

from scrappy_doo.evaluation import EvaluationTrace, ToolEvent
from scrappy_doo.execution import Execution
from scrappy_doo.knowledge import Knowledge, KnowledgeStore

if TYPE_CHECKING:
    from scrappy_doo.storage import ExecutionStore


class TraceRecorder:
    """Record action attempts before invocation and finalize durable results.

    An executed event means the handler was about to be invoked; it does not
    prove completion or describe external side effects.
    """

    def __init__(self, *, store: "ExecutionStore", execution_id: str):
        self.store = store
        self.execution_id = execution_id
        self._failed = False
        self.store.get_execution(execution_id)

    def mark_failed(self) -> None:
        self._failed = True

    def activate(self) -> None:
        with self.store._connect() as connection:
            self.store._load(connection, self.execution_id)
            connection.execute(
                "INSERT OR IGNORE INTO execution_traces (execution_id) VALUES (?)", (self.execution_id,)
            )
            cursor = connection.execute(
                "UPDATE execution_traces SET active = 1 WHERE execution_id = ? AND complete = 0",
                (self.execution_id,),
            )
            if not cursor.rowcount:
                raise ValueError("A completed trace cannot be restarted")

    def request_tool(self, name: str) -> int:
        with self.store._connect() as connection:
            active = connection.execute(
                "SELECT active, complete FROM execution_traces WHERE execution_id = ?", (self.execution_id,)
            ).fetchone()
            if active != (1, 0):
                raise ValueError("Trace is not active")
            cursor = connection.execute(
                "INSERT INTO execution_tool_events (execution_id, name) VALUES (?, ?)",
                (self.execution_id, name),
            )
            return cursor.lastrowid

    def start_tool(self, event_id: int) -> None:
        with self.store._connect() as connection:
            cursor = connection.execute(
                """UPDATE execution_tool_events SET authorized = 1, executed = 1
                   WHERE id = ? AND execution_id = ? AND executed = 0
                   AND EXISTS (SELECT 1 FROM execution_traces
                               WHERE execution_id = ? AND active = 1 AND complete = 0)""",
                (event_id, self.execution_id, self.execution_id),
            )
            if not cursor.rowcount:
                raise ValueError("Tool event cannot be started")

    def finish(self, execution: Execution) -> None:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.store._load(connection, self.execution_id)
            rows = connection.execute(
                """SELECT id, application, environment, text, source_execution_id,
                          source_deleted, evidence, conflicts_with, status
                   FROM knowledge WHERE source_execution_id = ? AND status = 'proposed' ORDER BY rowid""",
                (self.execution_id,),
            ).fetchall()
            proposals = [asdict(Knowledge(
                id=row[0], application=row[1], environment=row[2], text=row[3],
                source_execution_id=row[4], source_deleted=bool(row[5]),
                evidence=tuple(json.loads(row[6])), conflicts_with=tuple(json.loads(row[7])), status=row[8],
            )) for row in rows]
            connection.execute("UPDATE executions SET data = ? WHERE id = ?",
                               (self.store._serialize(execution), self.execution_id))
            if not self._failed:
                connection.execute(
                    """UPDATE execution_traces SET complete = 1, proposals = ?
                       WHERE execution_id = ? AND active = 1 AND complete = 0""",
                    (json.dumps(proposals), self.execution_id),
                )

    def snapshot(self) -> EvaluationTrace:
        with self.store._connect() as connection:
            execution = self.store._load(connection, self.execution_id)
            state = connection.execute(
                "SELECT complete, proposals FROM execution_traces WHERE execution_id = ?", (self.execution_id,)
            ).fetchone()
            events = connection.execute(
                """SELECT name, authorized, executed FROM execution_tool_events
                   WHERE execution_id = ? ORDER BY id""", (self.execution_id,)
            ).fetchall()
            ids = connection.execute(
                "SELECT knowledge_id FROM execution_knowledge WHERE execution_id = ? ORDER BY rowid",
                (self.execution_id,),
            ).fetchall()
            if state and state[0] and state[1] is not None:
                proposal_data = json.loads(state[1])
            else:
                rows = connection.execute(
                    "SELECT id FROM knowledge WHERE source_execution_id = ? AND status = 'proposed' ORDER BY rowid",
                    (self.execution_id,),
                ).fetchall()
                knowledge = KnowledgeStore(self.store.data_dir)
                proposal_data = [asdict(knowledge.get(row[0])) for row in rows]
        proposals = tuple(Knowledge(**dict(item, evidence=tuple(item["evidence"]),
                                           conflicts_with=tuple(item["conflicts_with"])))
                          for item in proposal_data)
        artifacts = {}
        root = self.store.data_dir / "executions" / self.execution_id / "artifacts"
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file():
                    name = path.relative_to(root).as_posix()
                    try:
                        artifacts[name] = self.store.read_artifact(self.execution_id, name)
                    except (OSError, ValueError):
                        continue
        return EvaluationTrace(
            execution_id=self.execution_id, execution=execution,
            tool_events=tuple(ToolEvent(name=row[0], authorized=bool(row[1]), executed=bool(row[2])) for row in events),
            artifacts=artifacts, knowledge_used=tuple(row[0] for row in ids),
            proposals=proposals, instrumentation_complete=bool(state and state[0]),
        )
