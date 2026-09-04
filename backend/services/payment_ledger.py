from dataclasses import dataclass
from datetime import datetime, timezone

from backend.services.database import database


# ============================================================
# PAYMENT RECORD
# ============================================================

@dataclass
class PaymentRecord:
    """
    Persistent representation of an AgentShield payment/order.
    """

    order_id: str

    payment_id: str | None

    status: str

    amount: int | None

    currency: str | None

    event: str

    received_at: str

    # AgentShield correlation
    agent_id: str | None = None

    session_id: str | None = None

    intent_id: str | None = None

    approval_id: str | None = None


# ============================================================
# PAYMENT LEDGER
# ============================================================

class PaymentLedger:
    """
    SQLite-backed persistent payment ledger.

    Stores:
        - Razorpay order
        - Razorpay payment
        - payment status
        - amount/currency
        - AgentShield agent
        - session
        - intent
        - approval

    Amount convention:
        amounts stored in the payments table are in paise
        (Razorpay's smallest currency unit).
    """

    def __init__(self):
        self._ensure_event_table()

    # ========================================================
    # EVENT IDEMPOTENCY TABLE
    # ========================================================

    def _ensure_event_table(self):
        """
        Create the processed-payment-events table if it does
        not already exist.

        This prevents processing the same webhook/event more
        than once.
        """

        connection = database.connect()

        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_payment_events (
                    event_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                )
                """
            )

            connection.commit()

        finally:
            connection.close()

    # ========================================================
    # REGISTER RAZORPAY ORDER
    # ========================================================

    def register_order(
        self,
        order_id: str,
        agent_id: str,
        session_id: str,
        intent_id: str,
        approval_id: str | None = None,
        amount: int | None = None,
        currency: str | None = None,
    ) -> PaymentRecord:
        """
        Register a newly-created Razorpay order.

        This stores enough AgentShield context to later
        correlate the Razorpay payment with the original
        AI-agent request.
        """

        received_at = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        connection = database.connect()

        try:
            # ------------------------------------------------
            # Check whether the order already exists
            # ------------------------------------------------

            existing = connection.execute(
                """
                SELECT *
                FROM payments
                WHERE order_id = ?
                """,
                (order_id,),
            ).fetchone()

            if existing:
                return self._row_to_model(
                    existing
                )

            # ------------------------------------------------
            # Insert new order
            # ------------------------------------------------

            connection.execute(
                """
                INSERT INTO payments (
                    order_id,
                    payment_id,
                    status,
                    amount,
                    currency,
                    event,
                    received_at,
                    agent_id,
                    session_id,
                    intent_id,
                    approval_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    None,
                    "ORDER_CREATED",
                    amount,
                    currency,
                    "order.created",
                    received_at,
                    agent_id,
                    session_id,
                    intent_id,
                    approval_id,
                ),
            )

            connection.commit()

        finally:
            connection.close()

        record = self.get(
            order_id
        )

        if record is None:
            raise RuntimeError(
                "Payment order was inserted "
                "but could not be retrieved."
            )

        return record

    # ========================================================
    # ADD PAYMENT EVENT
    # ========================================================

    def add_event(
        self,
        event_id: str,
        order_id: str,
        payment_id: str | None,
        status: str,
        amount: int | None,
        currency: str | None,
        event: str,
    ) -> bool:
        """
        Record a Razorpay event.

        Returns:
            True  -> event was processed
            False -> duplicate event was ignored
        """

        connection = database.connect()

        try:
            # ------------------------------------------------
            # Ensure idempotency table exists
            # ------------------------------------------------

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_payment_events (
                    event_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                )
                """
            )

            # ------------------------------------------------
            # Check duplicate event
            # ------------------------------------------------

            existing_event = connection.execute(
                """
                SELECT event_id
                FROM processed_payment_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()

            if existing_event:
                return False

            # ------------------------------------------------
            # Mark event as processed
            # ------------------------------------------------

            connection.execute(
                """
                INSERT INTO processed_payment_events (
                    event_id,
                    processed_at
                )
                VALUES (?, ?)
                """,
                (
                    event_id,
                    datetime.now(
                        timezone.utc
                    ).isoformat(),
                ),
            )

            # ------------------------------------------------
            # Retrieve AgentShield correlation
            # ------------------------------------------------

            existing_payment = connection.execute(
                """
                SELECT
                    agent_id,
                    session_id,
                    intent_id,
                    approval_id
                FROM payments
                WHERE order_id = ?
                """,
                (order_id,),
            ).fetchone()

            if existing_payment:

                agent_id = (
                    existing_payment[
                        "agent_id"
                    ]
                )

                session_id = (
                    existing_payment[
                        "session_id"
                    ]
                )

                intent_id = (
                    existing_payment[
                        "intent_id"
                    ]
                )

                approval_id = (
                    existing_payment[
                        "approval_id"
                    ]
                )

            else:

                agent_id = None
                session_id = None
                intent_id = None
                approval_id = None

            # ------------------------------------------------
            # Insert/update payment record
            # ------------------------------------------------

            connection.execute(
                """
                INSERT OR REPLACE INTO payments (
                    order_id,
                    payment_id,
                    status,
                    amount,
                    currency,
                    event,
                    received_at,
                    agent_id,
                    session_id,
                    intent_id,
                    approval_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    payment_id,
                    status,
                    amount,
                    currency,
                    event,
                    datetime.now(
                        timezone.utc
                    ).isoformat(),
                    agent_id,
                    session_id,
                    intent_id,
                    approval_id,
                ),
            )

            connection.commit()

            return True

        finally:
            connection.close()

    # ========================================================
    # GET PAYMENT BY ORDER ID
    # ========================================================

    def get(
        self,
        order_id: str,
    ) -> PaymentRecord | None:
        """
        Retrieve a payment/order by Razorpay order ID.
        """

        connection = database.connect()

        try:

            row = connection.execute(
                """
                SELECT *
                FROM payments
                WHERE order_id = ?
                """,
                (order_id,),
            ).fetchone()

        finally:
            connection.close()

        if row is None:
            return None

        return self._row_to_model(
            row
        )

    # ========================================================
    # FIND PAYMENT BY APPROVAL
    # ========================================================

    def find_by_approval(
        self,
        approval_id: str,
    ) -> PaymentRecord | None:
        """
        Find the payment order associated with an approval.

        Used to enforce one-time approvals.
        """

        connection = database.connect()

        try:

            row = connection.execute(
                """
                SELECT *
                FROM payments
                WHERE approval_id = ?
                ORDER BY received_at DESC
                LIMIT 1
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
    # SERVER-SIDE DAILY SPENDING
    # ========================================================

    def get_daily_spending(
        self,
        agent_id: str,
    ) -> int:
        """
        Calculate actual successful spending for the current
        UTC day directly from the persisted payment ledger.

        Returns:
            Total amount in paise.

        Important:
            This does NOT trust the amount_spent_today value
            supplied by an AI agent.
        """

        today = (
            datetime.now(
                timezone.utc
            ).date().isoformat()
        )

        connection = database.connect()

        try:

            row = connection.execute(
                """
                SELECT COALESCE(
                    SUM(amount),
                    0
                ) AS total
                FROM payments
                WHERE agent_id = ?
                AND received_at LIKE ?
                AND status IN (
                    'CAPTURED',
                    'PAID',
                    'SUCCESS',
                    'SIGNATURE_VERIFIED',
                    'AUTHORIZED'
                )
                """,
                (
                    agent_id,
                    f"{today}%",
                ),
            ).fetchone()

        finally:
            connection.close()

        if row is None:
            return 0

        return int(
            row["total"] or 0
        )

    # ========================================================
    # LIST ALL PAYMENTS
    # ========================================================

    def list_all(
        self,
    ) -> list[PaymentRecord]:
        """
        Return all stored payment records.
        """

        connection = database.connect()

        try:

            rows = connection.execute(
                """
                SELECT *
                FROM payments
                ORDER BY received_at DESC
                """
            ).fetchall()

        finally:
            connection.close()

        return [
            self._row_to_model(row)
            for row in rows
        ]

    # ========================================================
    # UPDATE PAYMENT STATUS
    # ========================================================

    def update_status(
        self,
        order_id: str,
        status: str,
        payment_id: str | None = None,
        event: str = "status.update",
        amount: int | None = None,
        currency: str | None = None,
    ) -> PaymentRecord | None:
        """
        Update an existing payment record directly.

        Useful for controlled internal updates.
        """

        existing = self.get(
            order_id
        )

        if existing is None:
            return None

        connection = database.connect()

        try:

            connection.execute(
                """
                UPDATE payments
                SET
                    payment_id = ?,
                    status = ?,
                    amount = ?,
                    currency = ?,
                    event = ?,
                    received_at = ?
                WHERE order_id = ?
                """,
                (
                    payment_id
                    if payment_id is not None
                    else existing.payment_id,

                    status,

                    amount
                    if amount is not None
                    else existing.amount,

                    currency
                    if currency is not None
                    else existing.currency,

                    event,

                    datetime.now(
                        timezone.utc
                    ).isoformat(),

                    order_id,
                ),
            )

            connection.commit()

        finally:
            connection.close()

        return self.get(
            order_id
        )

    # ========================================================
    # CLEAR
    # ========================================================

    def clear(self) -> None:
        """
        Delete all payment records and processed event IDs.

        Use only for local development/testing.
        """

        connection = database.connect()

        try:

            connection.execute(
                "DELETE FROM payments"
            )

            connection.execute(
                """
                DELETE FROM processed_payment_events
                """
            )

            connection.commit()

        finally:
            connection.close()

    # ========================================================
    # ROW CONVERSION
    # ========================================================

    @staticmethod
    def _row_to_model(
        row,
    ) -> PaymentRecord:

        return PaymentRecord(

            order_id=
                row["order_id"],

            payment_id=
                row["payment_id"],

            status=
                row["status"],

            amount=
                row["amount"],

            currency=
                row["currency"],

            event=
                row["event"],

            received_at=
                row["received_at"],

            agent_id=
                row["agent_id"],

            session_id=
                row["session_id"],

            intent_id=
                row["intent_id"],

            approval_id=
                row["approval_id"],
        )


# ============================================================
# GLOBAL PAYMENT LEDGER
# ============================================================

payment_ledger = PaymentLedger()