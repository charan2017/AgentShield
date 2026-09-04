import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.services.database import database


# ============================================================
# LEDGER EVENT
# ============================================================

@dataclass
class LedgerEvent:
    event_id: str
    timestamp: str
    event_type: str
    agent_id: str
    session_id: str
    intent_id: str
    message: str
    metadata: dict[str, Any]


# ============================================================
# ACTION LEDGER
# ============================================================

class ActionLedger:
    """
    Persistent SQLite-backed AgentShield audit ledger.
    """

    def __init__(self):
        # Make sure the database/table exists.
        self._ensure_table()

    # ========================================================
    # TABLE
    # ========================================================

    def _ensure_table(self):

        connection = database.connect()

        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS action_ledger (
                    event_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    intent_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    metadata TEXT NOT NULL
                )
                """
            )

            connection.commit()

        finally:
            connection.close()

    # ========================================================
    # RECORD EVENT
    # ========================================================

    def record(
        self,
        event_type: str,
        agent_id: str,
        session_id: str,
        intent_id: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> LedgerEvent:

        event_id = (
            f"EVT-"
            f"{uuid.uuid4().hex[:12].upper()}"
        )

        timestamp = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        event_metadata = metadata or {}
        safe_intent_id = str(intent_id).strip() if intent_id is not None else "NONE"
        if not safe_intent_id:
            safe_intent_id = "NONE"

        connection = database.connect()

        try:
            connection.execute(
                """
                INSERT INTO action_ledger (
                    event_id,
                    timestamp,
                    event_type,
                    agent_id,
                    session_id,
                    intent_id,
                    message,
                    metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    timestamp,
                    event_type,
                    agent_id,
                    session_id,
                    intent_id,
                    message,
                    json.dumps(
                        event_metadata,
                        default=str,
                    ),
                ),
            )

            connection.commit()

        finally:
            connection.close()

        return LedgerEvent(
            event_id=event_id,
            timestamp=timestamp,
            event_type=event_type,
            agent_id=agent_id,
            session_id=session_id,
            intent_id=intent_id,
            message=message,
            metadata=event_metadata,
        )

    # ========================================================
    # LIST ALL
    # ========================================================

    def list_all(self) -> list[dict[str, Any]]:

        connection = database.connect()

        try:
            rows = connection.execute(
                """
                SELECT *
                FROM action_ledger
                ORDER BY timestamp ASC
                """
            ).fetchall()

        finally:
            connection.close()

        return [
            self._row_to_dict(row)
            for row in rows
        ]

    # ========================================================
    # LIST BY SESSION
    # ========================================================

    def list_session(
        self,
        agent_id: str,
        session_id: str,
    ) -> list[dict[str, Any]]:

        connection = database.connect()

        try:
            rows = connection.execute(
                """
                SELECT *
                FROM action_ledger
                WHERE agent_id = ?
                AND session_id = ?
                ORDER BY timestamp ASC
                """,
                (
                    agent_id,
                    session_id,
                ),
            ).fetchall()

        finally:
            connection.close()

        return [
            self._row_to_dict(row)
            for row in rows
        ]

    # ========================================================
    # LIST BY INTENT
    # ========================================================

    def list_intent(
        self,
        intent_id: str,
    ) -> list[dict[str, Any]]:

        connection = database.connect()

        try:
            rows = connection.execute(
                """
                SELECT *
                FROM action_ledger
                WHERE intent_id = ?
                ORDER BY timestamp ASC
                """,
                (intent_id,),
            ).fetchall()

        finally:
            connection.close()

        return [
            self._row_to_dict(row)
            for row in rows
        ]

    # ========================================================
    # CLEAR
    # ========================================================

    def clear(self) -> None:

        connection = database.connect()

        try:
            connection.execute(
                "DELETE FROM action_ledger"
            )

            connection.commit()

        finally:
            connection.close()

    # ========================================================
    # ROW → DICT
    # ========================================================

    @staticmethod
    def _row_to_dict(row) -> dict[str, Any]:

        try:
            metadata = json.loads(
                row["metadata"]
            )
        except (
            TypeError,
            json.JSONDecodeError,
        ):
            metadata = {}

        return {
            "event_id":
                row["event_id"],

            "timestamp":
                row["timestamp"],

            "event_type":
                row["event_type"],

            "agent_id":
                row["agent_id"],

            "session_id":
                row["session_id"],

            "intent_id":
                row["intent_id"],

            "message":
                row["message"],

            "metadata":
                metadata,
        }


# ============================================================
# GLOBAL INSTANCE
# ============================================================

action_ledger = ActionLedger()