import hashlib
import json
from datetime import datetime, timezone

from backend.services.database import database


class IdempotencyService:
    """
    Prevents the same agent payment request from creating
    multiple Razorpay orders.

    The idempotency key and request hash are stored in SQLite.
    """

    def create_request_hash(
        self,
        payload: dict,
    ) -> str:
        """
        Create a stable SHA-256 hash of the payment request.
        """

        normalized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

        return hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()

    def get(
        self,
        idempotency_key: str,
    ):
        """
        Retrieve an existing idempotency record.
        """

        connection = database.connect()

        try:
            return connection.execute(
                """
                SELECT *
                FROM idempotency_keys
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()

        finally:
            connection.close()

    def reserve(
        self,
        idempotency_key: str,
        agent_id: str,
        intent_id: str,
        request_hash: str,
    ) -> bool:
        """
        Reserve an idempotency key.

        Returns:
            True  -> newly reserved
            False -> already exists
        """

        connection = database.connect()

        try:
            existing = connection.execute(
                """
                SELECT idempotency_key
                FROM idempotency_keys
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()

            if existing:
                return False

            connection.execute(
                """
                INSERT INTO idempotency_keys (
                    idempotency_key,
                    agent_id,
                    intent_id,
                    request_hash,
                    order_id,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    agent_id,
                    intent_id,
                    request_hash,
                    None,
                    "PROCESSING",
                    datetime.now(
                        timezone.utc
                    ).isoformat(),
                ),
            )

            connection.commit()

            return True

        finally:
            connection.close()

    def complete(
        self,
        idempotency_key: str,
        order_id: str,
    ) -> None:
        """
        Mark the idempotent request as completed and
        associate it with the created Razorpay order.
        """

        connection = database.connect()

        try:
            connection.execute(
                """
                UPDATE idempotency_keys
                SET
                    order_id = ?,
                    status = 'COMPLETED'
                WHERE idempotency_key = ?
                """,
                (
                    order_id,
                    idempotency_key,
                ),
            )

            connection.commit()

        finally:
            connection.close()

    def fail(
        self,
        idempotency_key: str,
    ) -> None:
        """
        Mark the request as failed.
        """

        connection = database.connect()

        try:
            connection.execute(
                """
                UPDATE idempotency_keys
                SET status = 'FAILED'
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            )

            connection.commit()

        finally:
            connection.close()


idempotency_service = IdempotencyService()