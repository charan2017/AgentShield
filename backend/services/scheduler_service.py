from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from zoneinfo import ZoneInfo

from backend.services.action_ledger import action_ledger
from backend.services.database import database


APP_TIMEZONE = ZoneInfo("Asia/Kolkata")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_datetime(value: str, default_timezone: ZoneInfo = APP_TIMEZONE) -> datetime:
    raw = value.strip()
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_timezone)
    return parsed.astimezone(timezone.utc)


def ensure_scheduler_tables() -> None:
    connection = database.connect()
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_payments (
                schedule_id TEXT PRIMARY KEY,
                intent_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                recipient TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                intended_amount REAL NOT NULL,
                intended_recipient TEXT NOT NULL,
                category TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
                status TEXT NOT NULL,
                decision TEXT,
                approval_id TEXT,
                order_id TEXT,
                reasons_json TEXT NOT NULL DEFAULT '[]',
                request_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                executed_at TEXT,
                last_error TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scheduled_payments_due
            ON scheduled_payments(status, scheduled_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS recurring_bills (
                bill_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                recipient TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'INR',
                day_of_month INTEGER NOT NULL,
                reminder_days_before INTEGER NOT NULL DEFAULT 1,
                next_due_at TEXT NOT NULL,
                next_reminder_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at TEXT NOT NULL,
                last_reminder_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recurring_bills_reminders
            ON recurring_bills(status, next_reminder_at)
            """
        )
        connection.commit()
    finally:
        connection.close()


ensure_scheduler_tables()


class HeaderOnlyRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def row_to_schedule(row: Any) -> dict[str, Any]:
    return {
        "schedule_id": row["schedule_id"],
        "intent_id": row["intent_id"],
        "agent_id": row["agent_id"],
        "session_id": row["session_id"],
        "recipient": row["recipient"],
        "amount": float(row["amount"]),
        "currency": row["currency"],
        "intended_amount": float(row["intended_amount"]),
        "intended_recipient": row["intended_recipient"],
        "category": row["category"],
        "scheduled_at": row["scheduled_at"],
        "timezone": row["timezone"],
        "status": row["status"],
        "decision": row["decision"],
        "approval_id": row["approval_id"],
        "order_id": row["order_id"],
        "reasons": json.loads(row["reasons_json"] or "[]"),
        "created_at": row["created_at"],
        "executed_at": row["executed_at"],
        "last_error": row["last_error"],
    }


def create_scheduled_payment(
    *,
    intent_id: str,
    agent_id: str,
    session_id: str,
    recipient: str,
    amount: float,
    currency: str,
    intended_amount: float,
    intended_recipient: str,
    category: str,
    scheduled_at: datetime,
    status: str,
    decision: str | None,
    approval_id: str | None,
    reasons: list[str],
    request_payload: dict[str, Any],
    timezone_name: str = "Asia/Kolkata",
) -> dict[str, Any]:
    schedule_id = f"SCH-{uuid.uuid4().hex[:14].upper()}"
    created_at = iso_utc(utc_now())

    connection = database.connect()
    try:
        connection.execute(
            """
            INSERT INTO scheduled_payments (
                schedule_id, intent_id, agent_id, session_id,
                recipient, amount, currency, intended_amount,
                intended_recipient, category, scheduled_at,
                timezone, status, decision, approval_id,
                reasons_json, request_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                schedule_id,
                intent_id,
                agent_id,
                session_id,
                recipient,
                amount,
                currency,
                intended_amount,
                intended_recipient,
                category,
                iso_utc(scheduled_at),
                timezone_name,
                status,
                decision,
                approval_id,
                json.dumps(reasons),
                json.dumps(request_payload),
                created_at,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    result = get_scheduled_payment(schedule_id)
    if not result:
        raise RuntimeError("Scheduled payment could not be persisted.")

    action_ledger.record(
        event_type="PAYMENT_SCHEDULED",
        agent_id=agent_id,
        session_id=session_id,
        intent_id=intent_id,
        message=f"Payment scheduled for {scheduled_at.astimezone(APP_TIMEZONE).strftime('%d %b %Y, %I:%M %p')} IST.",
        metadata={
            "schedule_id": schedule_id,
            "amount": amount,
            "recipient": recipient,
            "status": status,
            "decision": decision,
            "approval_id": approval_id,
        },
    )

    return result


def list_scheduled_payments(limit: int = 100) -> list[dict[str, Any]]:
    ensure_scheduler_tables()
    connection = database.connect()
    try:
        rows = connection.execute(
            """
            SELECT * FROM scheduled_payments
            ORDER BY scheduled_at ASC
            LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    finally:
        connection.close()
    return [row_to_schedule(row) for row in rows]


def get_scheduled_payment(schedule_id: str) -> dict[str, Any] | None:
    ensure_scheduler_tables()
    connection = database.connect()
    try:
        row = connection.execute(
            "SELECT * FROM scheduled_payments WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
    finally:
        connection.close()
    return row_to_schedule(row) if row else None


def update_scheduled_payment(
    schedule_id: str,
    *,
    status: str | None = None,
    decision: str | None = None,
    approval_id: str | None = None,
    order_id: str | None = None,
    reasons: list[str] | None = None,
    executed_at: str | None = None,
    last_error: str | None = None,
) -> dict[str, Any] | None:
    fields: list[str] = []
    values: list[Any] = []

    mapping = [
        ("status", status),
        ("decision", decision),
        ("approval_id", approval_id),
        ("order_id", order_id),
        ("executed_at", executed_at),
        ("last_error", last_error),
    ]
    for field, value in mapping:
        if value is not None:
            fields.append(f"{field} = ?")
            values.append(value)

    if reasons is not None:
        fields.append("reasons_json = ?")
        values.append(json.dumps(reasons))

    if not fields:
        return get_scheduled_payment(schedule_id)

    values.append(schedule_id)
    connection = database.connect()
    try:
        connection.execute(
            f"UPDATE scheduled_payments SET {', '.join(fields)} WHERE schedule_id = ?",
            tuple(values),
        )
        connection.commit()
    finally:
        connection.close()

    return get_scheduled_payment(schedule_id)


def cancel_scheduled_payment(schedule_id: str) -> dict[str, Any] | None:
    item = get_scheduled_payment(schedule_id)
    if not item:
        return None

    if item["status"] in {"COMPLETED", "BLOCKED", "CANCELLED"}:
        return item

    return update_scheduled_payment(
        schedule_id,
        status="CANCELLED",
    )


def row_to_bill(row: Any) -> dict[str, Any]:
    return {
        "bill_id": row["bill_id"],
        "name": row["name"],
        "recipient": row["recipient"],
        "amount": float(row["amount"]),
        "currency": row["currency"],
        "day_of_month": int(row["day_of_month"]),
        "reminder_days_before": int(row["reminder_days_before"]),
        "next_due_at": row["next_due_at"],
        "next_reminder_at": row["next_reminder_at"],
        "status": row["status"],
        "created_at": row["created_at"],
        "last_reminder_at": row["last_reminder_at"],
    }


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    current = datetime(year, month, 1, tzinfo=timezone.utc)
    return (next_month - current).days


def make_monthly_due(day_of_month: int | None = None, after: datetime | None = None) -> datetime:
    now_local = (after or utc_now()).astimezone(APP_TIMEZONE)
    year = now_local.year
    month = now_local.month
    resolved_day = int(day_of_month) if day_of_month is not None else now_local.day
    day = min(max(1, resolved_day), _days_in_month(year, month))
    candidate = datetime(year, month, day, 9, 0, tzinfo=APP_TIMEZONE)
    if candidate <= now_local:
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        day = min(max(1, resolved_day), _days_in_month(year, month))
        candidate = datetime(year, month, day, 9, 0, tzinfo=APP_TIMEZONE)
    return candidate.astimezone(timezone.utc)


def create_recurring_bill(
    *,
    name: str,
    recipient: str,
    amount: float,
    day_of_month: int | None = None,
    reminder_days_before: int = 1,
    currency: str = "INR",
) -> dict[str, Any]:
    now_local = utc_now().astimezone(APP_TIMEZONE)
    resolved_day = int(day_of_month) if day_of_month is not None else now_local.day
    due = make_monthly_due(resolved_day)
    reminder = due - timedelta(days=max(0, reminder_days_before))
    bill_id = f"BILL-{uuid.uuid4().hex[:12].upper()}"
    created_at = iso_utc(utc_now())

    connection = database.connect()
    try:
        connection.execute(
            """
            INSERT INTO recurring_bills (
                bill_id, name, recipient, amount, currency,
                day_of_month, reminder_days_before,
                next_due_at, next_reminder_at,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
            """,
            (
                bill_id,
                name,
                recipient,
                float(amount),
                currency,
                resolved_day,
                int(reminder_days_before),
                iso_utc(due),
                iso_utc(reminder),
                created_at,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    action_ledger.record(
        event_type="BILL_REMINDER_CREATED",
        agent_id="SYSTEM",
        session_id="BILLING",
        intent_id=bill_id,
        message=f"Monthly bill reminder created for {name}.",
        metadata={
            "bill_id": bill_id,
            "amount": amount,
            "recipient": recipient,
            "day_of_month": day_of_month,
            "reminder_days_before": reminder_days_before,
        },
    )

    return get_recurring_bill(bill_id)


def get_recurring_bill(bill_id: str) -> dict[str, Any] | None:
    ensure_scheduler_tables()
    connection = database.connect()
    try:
        row = connection.execute(
            "SELECT * FROM recurring_bills WHERE bill_id = ?",
            (bill_id,),
        ).fetchone()
    finally:
        connection.close()
    return row_to_bill(row) if row else None


def list_recurring_bills(limit: int = 100) -> list[dict[str, Any]]:
    ensure_scheduler_tables()
    connection = database.connect()
    try:
        rows = connection.execute(
            """
            SELECT * FROM recurring_bills
            ORDER BY next_due_at ASC
            LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    finally:
        connection.close()
    return [row_to_bill(row) for row in rows]


def cancel_recurring_bill(bill_id: str) -> dict[str, Any] | None:
    connection = database.connect()
    try:
        connection.execute(
            "UPDATE recurring_bills SET status = 'CANCELLED' WHERE bill_id = ?",
            (bill_id,),
        )
        connection.commit()
    finally:
        connection.close()
    return get_recurring_bill(bill_id)


def due_reminders(limit: int = 100) -> list[dict[str, Any]]:
    now = iso_utc(utc_now())
    connection = database.connect()
    try:
        rows = connection.execute(
            """
            SELECT * FROM recurring_bills
            WHERE status = 'ACTIVE' AND next_reminder_at <= ?
            ORDER BY next_reminder_at ASC
            LIMIT ?
            """,
            (now, max(1, min(int(limit), 500))),
        ).fetchall()
    finally:
        connection.close()
    return [row_to_bill(row) for row in rows]


def mark_bill_reminder_sent(bill_id: str) -> dict[str, Any] | None:
    item = get_recurring_bill(bill_id)
    if not item:
        return None

    current_due = parse_datetime(item["next_due_at"])
    next_due = make_monthly_due(item["day_of_month"], after=current_due + timedelta(minutes=1))
    next_reminder = next_due - timedelta(days=max(0, item["reminder_days_before"]))

    connection = database.connect()
    try:
        connection.execute(
            """
            UPDATE recurring_bills
            SET last_reminder_at = ?,
                next_due_at = ?,
                next_reminder_at = ?
            WHERE bill_id = ?
            """,
            (
                iso_utc(utc_now()),
                iso_utc(next_due),
                iso_utc(next_reminder),
                bill_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    action_ledger.record(
        event_type="BILL_REMINDER_TRIGGERED",
        agent_id="SYSTEM",
        session_id="BILLING",
        intent_id=bill_id,
        message=f"Reminder triggered for {item['name']}: ₹{item['amount']:.2f}.",
        metadata=item,
    )

    return get_recurring_bill(bill_id)


class SchedulerWorker:
    def __init__(self, execute_schedule: Callable[[dict[str, Any]], None], interval_seconds: int = 5):
        self.execute_schedule = execute_schedule
        self.interval_seconds = max(1, int(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="agentshield-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                action_ledger.record(
                    event_type="SCHEDULER_ERROR",
                    agent_id="SYSTEM",
                    session_id="SCHEDULER",
                    intent_id="SCHEDULER",
                    message="AgentShield scheduler loop encountered an error.",
                    metadata={"error": str(exc)},
                )
            self._stop.wait(self.interval_seconds)

    def tick(self) -> None:
        now = iso_utc(utc_now())
        connection = database.connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM scheduled_payments
                WHERE scheduled_at <= ?
                  AND status IN ('SCHEDULED', 'APPROVED')
                ORDER BY scheduled_at ASC
                LIMIT 20
                """,
                (now,),
            ).fetchall()
        finally:
            connection.close()

        for row in rows:
            item = row_to_schedule(row)
            try:
                update_scheduled_payment(item["schedule_id"], status="EXECUTING")
                self.execute_schedule(item)
            except Exception as exc:
                update_scheduled_payment(
                    item["schedule_id"],
                    status="FAILED",
                    last_error=str(exc),
                )
                action_ledger.record(
                    event_type="SCHEDULED_PAYMENT_FAILED",
                    agent_id=item["agent_id"],
                    session_id=item["session_id"],
                    intent_id=item["intent_id"],
                    message="Scheduled payment execution failed.",
                    metadata={
                        "schedule_id": item["schedule_id"],
                        "error": str(exc),
                    },
                )

        for bill in due_reminders(limit=20):
            mark_bill_reminder_sent(bill["bill_id"])
