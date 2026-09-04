from dataclasses import dataclass
from datetime import datetime, timezone
import json

from backend.services.database import database


# ============================================================
# BEHAVIOR RESULT
# ============================================================

@dataclass
class BehaviorResult:
    score: int
    level: str
    action: str
    repeated_attempts: int
    reason: str


# ============================================================
# BEHAVIOR ENGINE
# ============================================================

class BehaviorEngine:
    """
    Persistent AgentShield behavior monitoring engine.

    Tracks agent actions by:
        agent_id
        session_id

    The purpose is to detect patterns such as:
        - repeated payment attempts
        - rapid retries
        - suspicious escalation
        - payment loops
    """

    def __init__(self):
        self._ensure_table()

    # ========================================================
    # TABLE
    # ========================================================

    def _ensure_table(self):

        connection = database.connect()

        try:

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS behavior_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

            connection.commit()

        finally:

            connection.close()

    # ========================================================
    # EVALUATE
    # ========================================================

    def evaluate(
        self,
        agent_id: str,
        session_id: str,
        recipient: str,
        amount: float,
        category: str,
    ) -> BehaviorResult:

        now = datetime.now(
            timezone.utc
        )

        # ----------------------------------------------------
        # Load previous events for this session
        # ----------------------------------------------------

        connection = database.connect()

        try:

            rows = connection.execute(
                """
                SELECT
                    id,
                    agent_id,
                    session_id,
                    recipient,
                    amount,
                    category,
                    created_at
                FROM behavior_events
                WHERE agent_id = ?
                AND session_id = ?
                ORDER BY id ASC
                """,
                (
                    agent_id,
                    session_id,
                ),
            ).fetchall()

            # ------------------------------------------------
            # Record this attempt
            # ------------------------------------------------

            connection.execute(
                """
                INSERT INTO behavior_events (
                    agent_id,
                    session_id,
                    recipient,
                    amount,
                    category,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    session_id,
                    recipient,
                    amount,
                    category,
                    now.isoformat(),
                ),
            )

            connection.commit()

        finally:

            connection.close()

        # ====================================================
        # ATTEMPT COUNT
        # ====================================================

        repeated_attempts = (
            len(rows) + 1
        )

        # ====================================================
        # MATCHING HISTORY
        # ====================================================

        matching_attempts = 0

        for row in rows:

            same_recipient = (
                row["recipient"].strip().lower()
                == recipient.strip().lower()
            )

            same_amount = (
                float(row["amount"])
                == float(amount)
            )

            if same_recipient and same_amount:

                matching_attempts += 1

        # ====================================================
        # BEHAVIOR SCORE
        # ====================================================

        score = 0

        reasons: list[str] = []

        # ----------------------------------------------------
        # Repeated attempts
        # ----------------------------------------------------

        if repeated_attempts >= 2:

            score += 10

            reasons.append(
                "Agent has attempted multiple "
                "payments in the same session."
            )

        if repeated_attempts >= 3:

            score += 20

            reasons.append(
                "Repeated payment pattern detected."
            )

        if repeated_attempts >= 4:

            score += 30

            reasons.append(
                "Potential agent payment loop detected."
            )

        # ----------------------------------------------------
        # Same transaction repeated
        # ----------------------------------------------------

        if matching_attempts >= 1:

            score += 20

            reasons.append(
                "The agent is repeating the "
                "same payment request."
            )

        if matching_attempts >= 2:

            score += 20

            reasons.append(
                "Multiple identical payment "
                "attempts detected."
            )

        # ====================================================
        # LIMIT SCORE
        # ====================================================

        score = min(
            score,
            100,
        )

        # ====================================================
        # BEHAVIOR LEVEL
        # ====================================================

        if score >= 70:

            level = "HIGH"

        elif score >= 30:

            level = "MEDIUM"

        else:

            level = "LOW"

        # ====================================================
        # ACTION
        # ====================================================

        if score >= 70:

            action = "BLOCK"

        elif score >= 30:

            action = "REVIEW"

        else:

            action = "ALLOW"

        # ====================================================
        # REASON
        # ====================================================

        if reasons:

            reason = " ".join(
                reasons
            )

        else:

            reason = (
                "Agent behavior appears normal."
            )

        return BehaviorResult(
            score=score,
            level=level,
            action=action,
            repeated_attempts=repeated_attempts,
            reason=reason,
        )

    # ========================================================
    # SESSION EVENTS
    # ========================================================

    def get_session_events(
        self,
        agent_id: str,
        session_id: str,
    ):

        connection = database.connect()

        try:

            rows = connection.execute(
                """
                SELECT
                    id,
                    agent_id,
                    session_id,
                    recipient,
                    amount,
                    category,
                    created_at
                FROM behavior_events
                WHERE agent_id = ?
                AND session_id = ?
                ORDER BY id ASC
                """,
                (
                    agent_id,
                    session_id,
                ),
            ).fetchall()

        finally:

            connection.close()

        return [
            {
                "id":
                    row["id"],

                "agent_id":
                    row["agent_id"],

                "session_id":
                    row["session_id"],

                "recipient":
                    row["recipient"],

                "amount":
                    row["amount"],

                "category":
                    row["category"],

                "created_at":
                    row["created_at"],
            }
            for row in rows
        ]

    # ========================================================
    # CLEAR SESSION
    # ========================================================

    def clear_session(
        self,
        agent_id: str,
        session_id: str,
    ):

        connection = database.connect()

        try:

            connection.execute(
                """
                DELETE FROM behavior_events
                WHERE agent_id = ?
                AND session_id = ?
                """,
                (
                    agent_id,
                    session_id,
                ),
            )

            connection.commit()

        finally:

            connection.close()


# ============================================================
# GLOBAL INSTANCE
# ============================================================

behavior_engine = BehaviorEngine()