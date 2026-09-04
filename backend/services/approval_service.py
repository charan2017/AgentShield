from dataclasses import dataclass
from datetime import datetime, timezone
import json

from backend.services.database import database


# ============================================================
# APPROVAL RECORD
# ============================================================

@dataclass
class ApprovalRequest:
    approval_id: str
    intent_id: str
    agent_id: str
    session_id: str
    recipient: str
    amount: float
    currency: str
    risk_score: int
    risk_level: str
    reasons: list[str]
    status: str
    created_at: str
    approved_at: str | None = None


# ============================================================
# APPROVAL SERVICE
# ============================================================

class ApprovalService:

    # ========================================================
    # CREATE
    # ========================================================

    def create(
        self,
        approval_id: str,
        intent_id: str,
        agent_id: str,
        session_id: str,
        recipient: str,
        amount: float,
        currency: str,
        risk_score: int,
        risk_level: str,
        reasons: list[str],
    ) -> ApprovalRequest:

        created_at = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        connection = database.connect()

        try:

            connection.execute(
                """
                INSERT INTO approvals (
                    approval_id,
                    intent_id,
                    agent_id,
                    session_id,
                    recipient,
                    amount,
                    currency,
                    risk_score,
                    risk_level,
                    reasons,
                    status,
                    created_at,
                    approved_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    intent_id,
                    agent_id,
                    session_id,
                    recipient,
                    amount,
                    currency,
                    risk_score,
                    risk_level,
                    json.dumps(reasons),
                    "PENDING",
                    created_at,
                    None,
                ),
            )

            connection.commit()

        finally:
            connection.close()

        return self.get(
            approval_id
        )

    # ========================================================
    # GET
    # ========================================================

    def get(
        self,
        approval_id: str,
    ) -> ApprovalRequest | None:

        connection = database.connect()

        try:

            row = connection.execute(
                """
                SELECT *
                FROM approvals
                WHERE approval_id = ?
                """,
                (approval_id,),
            ).fetchone()

        finally:

            connection.close()

        if row is None:
            return None

        return self._row_to_model(
            row
        )

    # ========================================================
    # PENDING
    # ========================================================

    def list_pending(
        self,
    ) -> list[ApprovalRequest]:

        connection = database.connect()

        try:

            rows = connection.execute(
                """
                SELECT *
                FROM approvals
                WHERE status = 'PENDING'
                ORDER BY created_at DESC
                """
            ).fetchall()

        finally:

            connection.close()

        return [
            self._row_to_model(row)
            for row in rows
        ]

    # ========================================================
    # APPROVE
    # ========================================================

    def approve(
        self,
        approval_id: str,
    ) -> ApprovalRequest:

        approval = self.get(
            approval_id
        )

        if approval is None:

            raise ValueError(
                "Approval request not found."
            )

        if approval.status == "REJECTED":

            raise ValueError(
                "This approval request has already been rejected."
            )

        if approval.status == "APPROVED":

            return approval

        approved_at = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        connection = database.connect()

        try:

            cursor = connection.execute(
                """
                UPDATE approvals
                SET
                    status = 'APPROVED',
                    approved_at = ?
                WHERE approval_id = ?
                AND status = 'PENDING'
                """,
                (
                    approved_at,
                    approval_id,
                ),
            )

            connection.commit()

            if cursor.rowcount == 0:

                raise ValueError(
                    "Approval request could not be approved."
                )

        finally:

            connection.close()

        return self.get(
            approval_id
        )

    # ========================================================
    # REJECT
    # ========================================================

    def reject(
        self,
        approval_id: str,
    ) -> ApprovalRequest:

        approval = self.get(
            approval_id
        )

        if approval is None:

            raise ValueError(
                "Approval request not found."
            )

        if approval.status == "APPROVED":

            raise ValueError(
                "This payment has already been approved."
            )

        if approval.status == "REJECTED":

            return approval

        connection = database.connect()

        try:

            cursor = connection.execute(
                """
                UPDATE approvals
                SET status = 'REJECTED'
                WHERE approval_id = ?
                AND status = 'PENDING'
                """,
                (approval_id,),
            )

            connection.commit()

            if cursor.rowcount == 0:

                raise ValueError(
                    "Approval request could not be rejected."
                )

        finally:

            connection.close()

        return self.get(
            approval_id
        )

    # ========================================================
    # CONVERT ROW
    # ========================================================

    @staticmethod
    def _row_to_model(
        row,
    ) -> ApprovalRequest:

        reasons = json.loads(
            row["reasons"]
        )

        return ApprovalRequest(
            approval_id=row["approval_id"],
            intent_id=row["intent_id"],
            agent_id=row["agent_id"],
            session_id=row["session_id"],
            recipient=row["recipient"],
            amount=row["amount"],
            currency=row["currency"],
            risk_score=row["risk_score"],
            risk_level=row["risk_level"],
            reasons=reasons,
            status=row["status"],
            created_at=row["created_at"],
            approved_at=row["approved_at"],
        )


# ============================================================
# GLOBAL SERVICE
# ============================================================

approval_service = ApprovalService()