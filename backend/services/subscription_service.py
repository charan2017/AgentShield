from __future__ import annotations

import hmac
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.services.database import database
from backend.services.razorpay_service import get_razorpay_client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:14].upper()}"


def ensure_subscription_table() -> None:
    connection = database.connect()
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS recurring_subscriptions (
                subscription_request_id TEXT PRIMARY KEY,
                approval_id TEXT,
                intent_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                name TEXT NOT NULL,
                recipient TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                period TEXT NOT NULL,
                interval INTEGER NOT NULL,
                total_count INTEGER NOT NULL,
                plan_id TEXT,
                razorpay_subscription_id TEXT,
                status TEXT NOT NULL,
                authorization_payment_id TEXT,
                request_json TEXT,
                created_at TEXT NOT NULL,
                activated_at TEXT,
                cancelled_at TEXT,
                last_error TEXT
            )
            """
        )
        connection.commit()
    finally:
        connection.close()


def create_plan(
    *,
    name: str,
    amount: float,
    currency: str = "INR",
    period: str = "monthly",
    interval: int = 1,
    description: str | None = None,
    notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    client = get_razorpay_client()
    payload = {
        "period": period,
        "interval": interval,
        "item": {
            "name": name,
            "amount": int(round(amount * 100)),
            "currency": currency,
            "description": description or f"AgentShield recurring payment - {name}",
        },
        "notes": notes or {},
    }
    return client.plan.create(payload)


def create_razorpay_subscription(
    *,
    plan_id: str,
    total_count: int = 12,
    quantity: int = 1,
    start_at: int | None = None,
    notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    client = get_razorpay_client()
    payload: dict[str, Any] = {
        "plan_id": plan_id,
        "total_count": total_count,
        "quantity": quantity,
        "customer_notify": True,
        "notes": notes or {},
    }
    if start_at is not None:
        payload["start_at"] = start_at
    return client.subscription.create(payload)


def create_local_record(
    *,
    approval_id: str | None,
    intent_id: str,
    agent_id: str,
    session_id: str,
    name: str,
    recipient: str,
    amount: float,
    currency: str,
    period: str,
    interval: int,
    total_count: int,
    plan_id: str | None,
    razorpay_subscription_id: str | None,
    status: str,
    request_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_subscription_table()
    local_id = _new_id("RSUB")
    created_at = _now()
    connection = database.connect()
    try:
        connection.execute(
            """
            INSERT INTO recurring_subscriptions (
                subscription_request_id, approval_id, intent_id,
                agent_id, session_id, name, recipient, amount,
                currency, period, interval, total_count, plan_id,
                razorpay_subscription_id, status, authorization_payment_id,
                request_json, created_at, activated_at, cancelled_at,
                last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                local_id,
                approval_id,
                intent_id,
                agent_id,
                session_id,
                name,
                recipient,
                float(amount),
                currency,
                period,
                int(interval),
                int(total_count),
                plan_id,
                razorpay_subscription_id,
                status,
                None,
                json.dumps(request_payload or {}, default=str),
                created_at,
                None,
                None,
                None,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return get_subscription(local_id)


def update_subscription(
    subscription_request_id: str,
    *,
    status: str | None = None,
    razorpay_subscription_id: str | None = None,
    plan_id: str | None = None,
    authorization_payment_id: str | None = None,
    activated_at: str | None = None,
    cancelled_at: str | None = None,
    last_error: str | None = None,
) -> dict[str, Any] | None:
    ensure_subscription_table()
    fields = []
    values: list[Any] = []
    mapping = [
        ("status", status),
        ("razorpay_subscription_id", razorpay_subscription_id),
        ("plan_id", plan_id),
        ("authorization_payment_id", authorization_payment_id),
        ("activated_at", activated_at),
        ("cancelled_at", cancelled_at),
        ("last_error", last_error),
    ]
    for column, value in mapping:
        if value is not None:
            fields.append(f"{column} = ?")
            values.append(value)
    if not fields:
        return get_subscription(subscription_request_id)
    values.append(subscription_request_id)
    connection = database.connect()
    try:
        connection.execute(
            f"UPDATE recurring_subscriptions SET {', '.join(fields)} WHERE subscription_request_id = ?",
            values,
        )
        connection.commit()
    finally:
        connection.close()
    return get_subscription(subscription_request_id)


def get_subscription(subscription_request_id: str) -> dict[str, Any] | None:
    ensure_subscription_table()
    connection = database.connect()
    try:
        row = connection.execute(
            "SELECT * FROM recurring_subscriptions WHERE subscription_request_id = ?",
            (subscription_request_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    item = dict(row)
    try:
        item["request_payload"] = json.loads(item.get("request_json") or "{}")
    except json.JSONDecodeError:
        item["request_payload"] = {}
    item.pop("request_json", None)
    return item


def get_by_approval_id(approval_id: str) -> dict[str, Any] | None:
    ensure_subscription_table()
    connection = database.connect()
    try:
        row = connection.execute(
            "SELECT * FROM recurring_subscriptions WHERE approval_id = ? ORDER BY created_at DESC LIMIT 1",
            (approval_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return get_subscription(row["subscription_request_id"])


def list_subscriptions(limit: int = 100) -> list[dict[str, Any]]:
    ensure_subscription_table()
    connection = database.connect()
    try:
        rows = connection.execute(
            "SELECT * FROM recurring_subscriptions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    return [get_subscription(row["subscription_request_id"]) for row in rows]


def delete_local_request(subscription_request_id: str) -> None:
    ensure_subscription_table()
    connection = database.connect()
    try:
        connection.execute(
            "DELETE FROM recurring_subscriptions WHERE subscription_request_id = ?",
            (subscription_request_id,),
        )
        connection.commit()
    finally:
        connection.close()


def verify_subscription_signature(
    *,
    razorpay_payment_id: str,
    razorpay_subscription_id: str,
    razorpay_signature: str,
) -> bool:
    secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not secret:
        raise RuntimeError("RAZORPAY_KEY_SECRET is not configured.")
    message = f"{razorpay_payment_id}|{razorpay_subscription_id}".encode()
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, razorpay_signature)
