import sqlite3
from pathlib import Path
from threading import Lock


# ============================================================
# DATABASE LOCATION
# ============================================================

# Project root:
#
# AgentShield/
# ├── agentshield.db
# ├── backend/
# │   └── services/
# │       └── database.py
# └── frontend/
#
BASE_DIR = Path(__file__).resolve().parents[2]

DATABASE_PATH = BASE_DIR / "agentshield.db"


# ============================================================
# DATABASE SERVICE
# ============================================================

class Database:
    """
    SQLite database service for AgentShield.

    The database file is created automatically at:

        AgentShield/agentshield.db
    """

    def __init__(self):
        self.path = DATABASE_PATH
        self.lock = Lock()

        self.initialize()

    # ========================================================
    # CONNECTION
    # ========================================================

    def connect(self):
        """
        Create a SQLite connection.

        Row factory allows access like:

            row["agent_id"]
        """

        connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
        )

        connection.row_factory = sqlite3.Row

        # Improve SQLite reliability when multiple requests
        # happen close together.
        connection.execute(
            "PRAGMA foreign_keys = ON"
        )

        return connection

    # ========================================================
    # INITIALIZE DATABASE
    # ========================================================

    def initialize(self):
        """
        Create all AgentShield database tables if they do
        not already exist.
        """

        with self.lock:

            connection = self.connect()

            try:

                cursor = connection.cursor()

                # =================================================
                # APPROVALS
                # =================================================

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS approvals (
                        approval_id TEXT PRIMARY KEY,

                        intent_id TEXT NOT NULL,

                        agent_id TEXT NOT NULL,

                        session_id TEXT NOT NULL,

                        recipient TEXT NOT NULL,

                        amount REAL NOT NULL,

                        currency TEXT NOT NULL,

                        risk_score INTEGER NOT NULL,

                        risk_level TEXT NOT NULL,

                        reasons TEXT NOT NULL,

                        status TEXT NOT NULL,

                        created_at TEXT NOT NULL,

                        approved_at TEXT
                    )
                    """
                )

                # =================================================
                # PAYMENTS
                # =================================================

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS payments (
                        order_id TEXT PRIMARY KEY,

                        payment_id TEXT,

                        status TEXT NOT NULL,

                        amount INTEGER,

                        currency TEXT,

                        event TEXT NOT NULL,

                        received_at TEXT NOT NULL,

                        agent_id TEXT,

                        session_id TEXT,

                        intent_id TEXT,

                        approval_id TEXT
                    )
                    """
                )

                # =================================================
                # ACTION LEDGER
                # =================================================

                cursor.execute(
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

                # =================================================
                # BEHAVIOR EVENTS
                # =================================================

                cursor.execute(
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

                # =================================================
                # PROCESSED PAYMENT EVENTS
                # =================================================
                #
                # Used for webhook/event idempotency.
                #
                # Example:
                #
                # Razorpay sends the same webhook twice.
                #
                # First:
                #   processed ✅
                #
                # Second:
                #   ignored ✅
                #
                # =================================================

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS processed_payment_events (
                        event_id TEXT PRIMARY KEY,

                        processed_at TEXT NOT NULL
                    )
                    """
                )

                # =================================================
                # IDEMPOTENCY KEYS
                # =================================================
                #
                # Prevents an AI agent from accidentally creating
                # multiple payment orders for the same request.
                #
                # =================================================

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS idempotency_keys (
                        idempotency_key TEXT PRIMARY KEY,

                        agent_id TEXT NOT NULL,

                        intent_id TEXT NOT NULL,

                        request_hash TEXT NOT NULL,

                        order_id TEXT,

                        status TEXT NOT NULL,

                        created_at TEXT NOT NULL
                    )
                    """
                )

                # =================================================
                # INDEXES
                # =================================================

                # Approval lookup
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_approvals_status
                    ON approvals(status)
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_approvals_agent
                    ON approvals(agent_id)
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_approvals_intent
                    ON approvals(intent_id)
                    """
                )

                # Payment lookup
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_payments_agent
                    ON payments(agent_id)
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_payments_intent
                    ON payments(intent_id)
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_payments_approval
                    ON payments(approval_id)
                    """
                )

                # Action ledger lookup
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_action_agent_session
                    ON action_ledger(
                        agent_id,
                        session_id
                    )
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_action_intent
                    ON action_ledger(intent_id)
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_action_type
                    ON action_ledger(event_type)
                    """
                )

                # Behavior lookup
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_behavior_agent_session
                    ON behavior_events(
                        agent_id,
                        session_id
                    )
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_behavior_created
                    ON behavior_events(created_at)
                    """
                )

                # =================================================
                # COMMIT
                # =================================================

                connection.commit()

            finally:

                connection.close()


# ============================================================
# GLOBAL DATABASE INSTANCE
# ============================================================

database = Database()