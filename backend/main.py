import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.services.action_ledger import action_ledger
from backend.services.approval_service import approval_service
from backend.services.behavior_engine import behavior_engine
from backend.services.database import database
from backend.services.idempotency import idempotency_service
from backend.services.payment_ledger import payment_ledger
from backend.services.risk_engine import calculate_risk
from backend.services.command_parser import (
    parse_command as parse_agent_command,
)
from backend.services.scheduler_service import (
    create_scheduled_payment,
    create_recurring_bill,
    list_scheduled_payments,
    list_recurring_bills,
    get_scheduled_payment,
    update_scheduled_payment,
    SchedulerWorker,
)
from backend.services.razorpay_service import (
    create_order,
    get_razorpay_client,
    verify_payment_signature,
    verify_webhook_signature,
)
from backend.services.subscription_service import (
    create_plan,
    create_razorpay_subscription,
    create_local_record,
    get_by_approval_id,
    list_subscriptions,
    get_subscription,
    update_subscription,
    verify_subscription_signature,
)


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()

RAZORPAY_WEBHOOK_SECRET = os.getenv(
    "RAZORPAY_WEBHOOK_SECRET"
)


# ============================================================
# SERVER-SIDE SECURITY SETTINGS
# ============================================================

DEFAULT_SECURITY_SETTINGS = {
    "max_transaction_amount": 5000.0,
    "daily_limit": 15000.0,
    "approval_threshold": 3000.0,
}


def ensure_settings_table():
    connection = database.connect()
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS security_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value REAL NOT NULL
            )
            """
        )

        for key, value in DEFAULT_SECURITY_SETTINGS.items():
            connection.execute(
                """
                INSERT OR IGNORE INTO security_settings
                    (setting_key, setting_value)
                VALUES (?, ?)
                """,
                (key, value),
            )

        connection.commit()
    finally:
        connection.close()


def get_security_settings():
    ensure_settings_table()

    connection = database.connect()
    try:
        rows = connection.execute(
            """
            SELECT setting_key, setting_value
            FROM security_settings
            """
        ).fetchall()
    finally:
        connection.close()

    settings = dict(DEFAULT_SECURITY_SETTINGS)

    for row in rows:
        settings[row["setting_key"]] = float(
            row["setting_value"]
        )

    return settings


def update_security_settings(
    max_transaction_amount: float,
    daily_limit: float,
    approval_threshold: float,
):
    if approval_threshold > max_transaction_amount:
        raise ValueError(
            "Human approval threshold cannot exceed the transaction limit."
        )

    if daily_limit < max_transaction_amount:
        raise ValueError(
            "Daily spending limit must be at least the transaction limit."
        )

    values = {
        "max_transaction_amount": float(max_transaction_amount),
        "daily_limit": float(daily_limit),
        "approval_threshold": float(approval_threshold),
    }

    ensure_settings_table()

    connection = database.connect()
    try:
        for key, value in values.items():
            connection.execute(
                """
                UPDATE security_settings
                SET setting_value = ?
                WHERE setting_key = ?
                """,
                (value, key),
            )

        connection.commit()
    finally:
        connection.close()

    return values


# Initialize the settings table once when the API module loads.
ensure_settings_table()


# ============================================================
# SCHEDULER LIFECYCLE
# ============================================================

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execute_scheduled_payment(item: dict):
    """
    Execute one due scheduled payment.

    SCHEDULED payments are evaluated again through the same
    AgentShield create-payment pipeline immediately before a
    Razorpay order can be created.

    A REVIEW result is moved to AWAITING_APPROVAL. Once a human
    approves that request, the scheduler can finish it through
    the existing approved-payment flow.
    """
    schedule_id = item["schedule_id"]

    if item.get("status") == "APPROVED":
        approval_id = item.get("approval_id")
        if not approval_id:
            raise RuntimeError(
                "Scheduled payment is APPROVED but has no approval_id."
            )

        try:
            result = create_approved_payment(approval_id)
        except HTTPException as exc:
            # Another request may already have consumed this approval.
            existing_payment = payment_ledger.find_by_approval(approval_id)
            if existing_payment is not None:
                order_id = existing_payment.order_id
                update_scheduled_payment(
                    schedule_id,
                    status="COMPLETED",
                    decision="ALLOW",
                    order_id=order_id,
                    executed_at=_utc_now_iso(),
                    last_error=None,
                )
                return
            raise RuntimeError(str(exc.detail)) from exc

        if result.get("order_id"):
            update_scheduled_payment(
                schedule_id,
                status="COMPLETED",
                decision="ALLOW",
                order_id=result["order_id"],
                executed_at=_utc_now_iso(),
                last_error=None,
            )
            return

        raise RuntimeError(
            "Approved scheduled payment did not produce a Razorpay order."
        )

    request_payload = item.get("request_payload") or {}
    stored_merchant = request_payload.get("merchant") or item["recipient"]
    stored_category = request_payload.get("category") or item["category"]
    merchant_known = bool(request_payload.get("merchant_known", True))
    unusual = bool(request_payload.get("unusual", False))

    payment_request = PaymentRequest(
        intent_id=item["intent_id"],
        agent_id=item["agent_id"],
        session_id=item["session_id"],
        recipient=item["recipient"],
        amount=float(item["amount"]),
        currency=item["currency"],
        intended_amount=float(item["intended_amount"]),
        intended_recipient=item["intended_recipient"],
        max_transaction_amount=get_security_settings()[
            "max_transaction_amount"
        ],
        daily_limit=get_security_settings()["daily_limit"],
        amount_spent_today=0.0,
        previous_payment_same_request=False,
        merchant=stored_merchant,
        category=stored_category,
        merchant_known=merchant_known,
        unusual=unusual,
    )

    result = create_agent_payment(
        payment_request,
        _CommandRequestAdapter(
            f"SCHEDULE-{schedule_id}"
        ),
    )

    decision = result.get("decision")
    reasons = result.get("reasons") or []

    if decision == "ALLOW" and result.get("order_id"):
        update_scheduled_payment(
            schedule_id,
            status="COMPLETED",
            decision="ALLOW",
            order_id=result["order_id"],
            executed_at=_utc_now_iso(),
            reasons=reasons,
            last_error=None,
        )
        action_ledger.record(
            event_type="SCHEDULED_PAYMENT_COMPLETED",
            agent_id=item["agent_id"],
            session_id=item["session_id"],
            intent_id=item["intent_id"],
            message="Scheduled payment passed a fresh AgentShield check and a Razorpay order was created.",
            metadata={
                "schedule_id": schedule_id,
                "order_id": result["order_id"],
                "risk_score": result.get("risk_score"),
            },
        )
        return

    if decision == "REVIEW":
        approval_id = result.get("approval_id")
        update_scheduled_payment(
            schedule_id,
            status="AWAITING_APPROVAL",
            decision="REVIEW",
            approval_id=approval_id,
            reasons=reasons,
            last_error=None,
        )
        action_ledger.record(
            event_type="SCHEDULED_PAYMENT_REVIEW",
            agent_id=item["agent_id"],
            session_id=item["session_id"],
            intent_id=item["intent_id"],
            message="Scheduled payment requires human approval after execution-time security evaluation.",
            metadata={
                "schedule_id": schedule_id,
                "approval_id": approval_id,
                "risk_score": result.get("risk_score"),
            },
        )
        return

    if decision == "BLOCK":
        update_scheduled_payment(
            schedule_id,
            status="BLOCKED",
            decision="BLOCK",
            reasons=reasons,
            executed_at=_utc_now_iso(),
            last_error=None,
        )
        action_ledger.record(
            event_type="SCHEDULED_PAYMENT_BLOCKED",
            agent_id=item["agent_id"],
            session_id=item["session_id"],
            intent_id=item["intent_id"],
            message="Scheduled payment was blocked by AgentShield at execution time.",
            metadata={
                "schedule_id": schedule_id,
                "risk_score": result.get("risk_score"),
                "reasons": reasons,
            },
        )
        return

    raise RuntimeError(
        f"Unexpected scheduled payment decision: {decision}"
    )


@asynccontextmanager
async def app_lifespan(app):
    worker = SchedulerWorker(
        execute_schedule=_execute_scheduled_payment,
        interval_seconds=5,
    )
    worker.start()

    action_ledger.record(
        event_type="SCHEDULER_STARTED",
        agent_id="SYSTEM",
        session_id="SCHEDULER",
        intent_id="SCHEDULER",
        message="AgentShield scheduled-payment worker started.",
        metadata={"interval_seconds": 5},
    )

    try:
        yield
    finally:
        worker.stop()
        action_ledger.record(
            event_type="SCHEDULER_STOPPED",
            agent_id="SYSTEM",
            session_id="SCHEDULER",
            intent_id="SCHEDULER",
            message="AgentShield scheduled-payment worker stopped.",
            metadata={},
        )



# ============================================================
# APPLICATION
# ============================================================

app = FastAPI(
    lifespan=app_lifespan,
    title="AgentShield API",
    version="1.0.0",
    description=(
        "AI-agent payment security gateway with "
        "intent validation, policy enforcement, "
        "risk detection, behavior monitoring, "
        "human approval, Razorpay integration, "
        "persistent payment and action ledgers, "
        "server-side spending limits, and idempotency."
    ),
)

# ============================================================
# SCHEDULER TEST ENDPOINT
# ============================================================

class SchedulerTestRequest(BaseModel):
    amount: float = 100.0
    recipient: str = "Scheduler Test"
    delay_seconds: int = 20
    currency: str = "INR"


@app.post("/scheduler/test-payment")
def create_scheduler_test_payment(request: SchedulerTestRequest):
    """
    Development-only helper for validating the scheduler end-to-end.

    It creates a small scheduled payment a few seconds in the future.
    The scheduler will perform a fresh AgentShield security check when
    the payment becomes due and will create a Razorpay order only when
    the execution-time decision is ALLOW.
    """
    if request.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero.")

    if request.delay_seconds < 5 or request.delay_seconds > 3600:
        raise HTTPException(
            status_code=400,
            detail="delay_seconds must be between 5 and 3600.",
        )

    now_utc = datetime.now(timezone.utc)
    scheduled_at = now_utc + timedelta(seconds=request.delay_seconds)

    intent_id = f"SCHED-TEST-{uuid.uuid4().hex[:12].upper()}"
    agent_id = "SCHEDULER-TEST-AGENT"
    session_id = f"SCHEDULER-TEST-SESSION-{uuid.uuid4().hex[:10]}"

    request_payload = {
        "source": "scheduler-test-endpoint",
        "amount": request.amount,
        "currency": request.currency.upper(),
        "recipient": request.recipient,
        "merchant": request.recipient,
        "category": "scheduler_test",
        "merchant_known": True,
        "unusual": False,
        "agent_id": agent_id,
        "session_id": session_id,
        "intent_id": intent_id,
    }

    schedule = create_scheduled_payment(
        intent_id=intent_id,
        agent_id=agent_id,
        session_id=session_id,
        recipient=request.recipient,
        amount=request.amount,
        currency=request.currency.upper(),
        intended_amount=request.amount,
        intended_recipient=request.recipient,
        category="scheduler_test",
        scheduled_at=scheduled_at,
        status="SCHEDULED",
        decision="SCHEDULED",
        approval_id=None,
        reasons=[],
        request_payload=request_payload,
        timezone_name="Asia/Kolkata",
    )

    action_ledger.record(
        event_type="SCHEDULER_TEST_CREATED",
        agent_id=agent_id,
        session_id=session_id,
        intent_id=intent_id,
        message="Development scheduler test payment created.",
        metadata={
            "schedule_id": schedule["schedule_id"],
            "scheduled_at": schedule["scheduled_at"],
            "delay_seconds": request.delay_seconds,
            "amount": request.amount,
        },
    )

    return {
        "status": "scheduled",
        "message": (
            "Test payment scheduled. The worker will re-check security "
            "at execution time and create a Razorpay order only if allowed."
        ),
        "schedule": schedule,
    }



# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# REQUEST MODELS
# ============================================================

class AgentCommandRequest(BaseModel):
    command: str = Field(
        ...,
        min_length=1,
        max_length=1000,
    )
    agent_id: str = Field(
        default="CHAT-AGENT-001",
        min_length=1,
    )
    session_id: str | None = None
    intended_amount: float | None = Field(
        default=None,
        gt=0,
    )
    intended_recipient: str | None = None
    merchant: str | None = None
    category: str = Field(
        default="personal_transfer",
        min_length=1,
    )
    merchant_known: bool = True
    unusual: bool = False


class PaymentRequest(BaseModel):
    intent_id: str = Field(
        ...,
        min_length=1,
    )

    agent_id: str = Field(
        ...,
        min_length=1,
    )

    session_id: str = Field(
        default="default-session",
        min_length=1,
    )

    recipient: str = Field(
        ...,
        min_length=1,
    )

    amount: float = Field(
        ...,
        gt=0,
    )

    currency: str = "INR"

    intended_amount: float = Field(
        ...,
        gt=0,
    )

    intended_recipient: str = Field(
        ...,
        min_length=1,
    )

    max_transaction_amount: float = Field(
        ...,
        gt=0,
    )

    daily_limit: float = Field(
        ...,
        gt=0,
    )

    # Kept for compatibility with the frontend/API.
    # AgentShield does NOT trust this value.
    amount_spent_today: float = Field(
        default=0,
        ge=0,
    )

    previous_payment_same_request: bool = False

    merchant: str = Field(
        default="Demo Merchant",
        min_length=1,
    )

    category: str = Field(
        default="personal_transfer",
        min_length=1,
    )

    merchant_known: bool = True

    unusual: bool = False


class RiskDecision(BaseModel):
    decision: Literal[
        "ALLOW",
        "REVIEW",
        "BLOCK",
    ]

    risk_score: int = Field(
        ge=0,
        le=100,
    )

    reasons: list[str]

    remaining_daily_limit: float

    intent_match: bool

    authorization_valid: bool

    agent_behavior_score: int = 0

    agent_behavior_level: str = "LOW"

    repeated_attempts: int = 0

    agent_action: str = "ALLOW"

    approval_id: str | None = None


class SecuritySettingsUpdate(BaseModel):
    max_transaction_amount: float = Field(..., gt=0)
    daily_limit: float = Field(..., gt=0)
    approval_threshold: float = Field(..., ge=0)


class RecurringSubscriptionRequest(BaseModel):
    name: str = Field(..., min_length=1)
    recipient: str = Field(..., min_length=1)
    amount: float = Field(..., gt=0)
    currency: str = "INR"
    period: Literal["monthly"] = "monthly"
    interval: int = Field(default=1, ge=1, le=12)
    total_count: int = Field(default=12, ge=1, le=120)
    agent_id: str = Field(default="CHAT-AGENT-001", min_length=1)
    session_id: str = Field(default="default-session", min_length=1)
    intent_id: str | None = None
    intended_amount: float | None = Field(default=None, gt=0)
    intended_recipient: str | None = None
    category: str = "subscription"
    merchant_known: bool = True
    unusual: bool = False


class SubscriptionVerificationRequest(BaseModel):
    subscription_request_id: str = Field(..., min_length=1)
    razorpay_payment_id: str = Field(..., min_length=1)
    razorpay_subscription_id: str = Field(..., min_length=1)
    razorpay_signature: str = Field(..., min_length=1)


class PaymentVerificationRequest(BaseModel):
    razorpay_order_id: str = Field(
        ...,
        min_length=1,
    )

    razorpay_payment_id: str = Field(
        ...,
        min_length=1,
    )

    razorpay_signature: str = Field(
        ...,
        min_length=1,
    )


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():
    return {
        "name": "AgentShield",
        "status": "running",
        "version": "1.0.0",
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "agentshield-api",
    }


# ============================================================
# SECURITY SETTINGS
# ============================================================

@app.get("/settings")
def get_settings():
    return {
        "status": "ok",
        "settings": get_security_settings(),
    }


@app.put("/settings")
def update_settings(
    request: SecuritySettingsUpdate,
):
    try:
        settings = update_security_settings(
            max_transaction_amount=
                request.max_transaction_amount,
            daily_limit=request.daily_limit,
            approval_threshold=
                request.approval_threshold,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    action_ledger.record(
        event_type="SECURITY_SETTINGS_UPDATED",
        agent_id="SYSTEM",
        session_id="SYSTEM",
        intent_id="SETTINGS",
        message="AgentShield security settings updated.",
        metadata=settings,
    )

    return {
        "status": "updated",
        "settings": settings,
    }


# ============================================================
# DATABASE STATUS
# ============================================================

@app.get("/database/status")
def database_status():
    """
    Simple database health endpoint.
    """

    try:
        connection = database.connect()

        connection.execute(
            "SELECT 1"
        ).fetchone()

        connection.close()

        return {
            "status": "ok",
            "database": "sqlite",
            "path": str(database.path),
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Database check failed: "
                f"{str(exc)}"
            ),
        )


# ============================================================
# RAZORPAY AUTH TEST
# ============================================================

@app.get("/razorpay/test-auth")
def razorpay_test_auth():
    """
    Verify Razorpay API credentials.
    """

    try:
        client = get_razorpay_client()

        response = client.order.all(
            {
                "count": 1,
            }
        )

        return {
            "status": "success",
            "message": (
                "Razorpay authentication successful"
            ),
            "orders_found": len(
                response.get(
                    "items",
                    [],
                )
            ),
        }

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Razorpay authentication failed: "
                f"{str(exc)}"
            ),
        )


# ============================================================
# SECURITY EVALUATION
# ============================================================

@app.post(
    "/agent/payment-request",
    response_model=RiskDecision,
)
def payment_request(
    request: PaymentRequest,
):
    """
    Evaluate an AI-agent payment request.

    This endpoint does not create a Razorpay order.
    """

    reasons: list[str] = []

    risk_score = 0

    approval_id: str | None = None

    # Policy values are controlled by AgentShield on the server.
    settings = get_security_settings()

    server_max_transaction_amount = settings[
        "max_transaction_amount"
    ]

    server_daily_limit = settings[
        "daily_limit"
    ]

    server_approval_threshold = settings[
        "approval_threshold"
    ]

    # ========================================================
    # 1. USER INTENT VALIDATION
    # ========================================================

    amount_matches = (
        request.amount
        == request.intended_amount
    )

    recipient_matches = (
        request.recipient.strip().lower()
        == request.intended_recipient.strip().lower()
    )

    intent_match = (
        amount_matches
        and recipient_matches
    )

    if not amount_matches:
        risk_score += 50

        reasons.append(
            "Intent mismatch: "
            f"user authorized "
            f"₹{request.intended_amount:.2f}, "
            f"but the agent requested "
            f"₹{request.amount:.2f}."
        )

    if not recipient_matches:
        risk_score += 40

        reasons.append(
            "Recipient mismatch: "
            f"user authorized "
            f"'{request.intended_recipient}', "
            f"but the agent requested "
            f"'{request.recipient}'."
        )

    # ========================================================
    # 2. PER-TRANSACTION LIMIT
    # ========================================================

    authorization_valid = (
        request.amount
        <= server_max_transaction_amount
    )

    if not authorization_valid:
        risk_score += 50

        reasons.append(
            "Payment exceeds the agent's "
            "per-transaction authorization limit."
        )

    # ========================================================
    # 3. SERVER-SIDE DAILY SPENDING
    # ========================================================

    actual_spending_today_paise = (
        payment_ledger.get_daily_spending(
            request.agent_id
        )
    )

    actual_spending_today = (
        actual_spending_today_paise / 100
    )

    new_daily_total = (
        actual_spending_today
        + request.amount
    )

    daily_limit_exceeded = (
        new_daily_total
        > server_daily_limit
    )

    remaining_daily_limit = max(
        server_daily_limit
        - new_daily_total,
        0,
    )

    if daily_limit_exceeded:
        risk_score += 50

        reasons.append(
            "Daily spending limit would "
            "be exceeded: "
            f"actual spending today is "
            f"₹{actual_spending_today:.2f}, "
            f"and this request would bring "
            f"the total to "
            f"₹{new_daily_total:.2f} "
            f"against a limit of "
            f"₹{server_daily_limit:.2f}."
        )

    # ========================================================
    # 4. EXPLICIT DUPLICATE FLAG
    # ========================================================

    duplicate_detected = (
        request.previous_payment_same_request
    )

    if duplicate_detected:
        risk_score += 60

        reasons.append(
            "Duplicate payment request detected."
        )

    # ========================================================
    # 5. TRANSACTION RISK ENGINE
    # ========================================================

    transaction_risk = calculate_risk(
        amount=request.amount,
        category=request.category,
        merchant_known=request.merchant_known,
        unusual=request.unusual,
    )

    transaction_risk_score = (
        transaction_risk.get(
            "risk_score",
            0,
        )
    )

    risk_score += transaction_risk_score

    reasons.extend(
        transaction_risk.get(
            "risk_reasons",
            [],
        )
    )

    # ========================================================
    # 6. AGENT BEHAVIOR ENGINE
    # ========================================================

    behavior_result = (
        behavior_engine.evaluate(
            agent_id=request.agent_id,
            session_id=request.session_id,
            recipient=request.recipient,
            amount=request.amount,
            category=request.category,
        )
    )

    risk_score += behavior_result.score

    if behavior_result.score > 0:
        reasons.append(
            behavior_result.reason
        )

    risk_score = min(
        max(risk_score, 0),
        100,
    )

    # ========================================================
    # 7. HARD BLOCK CONDITIONS
    # ========================================================

    hard_block = (
        not intent_match
        or not authorization_valid
        or daily_limit_exceeded
        or duplicate_detected
        or (
            behavior_result.action
            == "BLOCK"
        )
    )

    # ========================================================
    # 8. FINAL DECISION
    # ========================================================

    if hard_block:

        decision = "BLOCK"

    elif request.amount > server_approval_threshold:

        decision = "REVIEW"

    elif risk_score >= 70:

        decision = "BLOCK"

    elif (
        risk_score >= 30
        or behavior_result.action
        == "REVIEW"
    ):

        decision = "REVIEW"

    else:

        decision = "ALLOW"

    # ========================================================
    # 9. DEFAULT ALLOW MESSAGE
    # ========================================================

    if (
        decision == "ALLOW"
        and not reasons
    ):

        reasons.append(
            "Payment matches the user's "
            "intent and authorization policy."
        )

    # ========================================================
    # 10. HUMAN APPROVAL
    # ========================================================

    if decision == "REVIEW":

        approval_id = (
            f"APR-"
            f"{uuid.uuid4().hex[:10].upper()}"
        )

        if risk_score >= 70:
            risk_level = "HIGH"

        elif risk_score >= 30:
            risk_level = "MEDIUM"

        else:
            risk_level = "LOW"

        approval_service.create(
            approval_id=approval_id,
            intent_id=request.intent_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            recipient=request.recipient,
            amount=request.amount,
            currency=request.currency,
            risk_score=risk_score,
            risk_level=risk_level,
            reasons=reasons,
        )

    # ========================================================
    # 11. AUDIT — REQUEST
    # ========================================================

    action_ledger.record(
        event_type="AGENT_REQUEST",
        agent_id=request.agent_id,
        session_id=request.session_id,
        intent_id=request.intent_id,
        message=(
            f"Agent requested ₹"
            f"{request.amount:.2f} "
            f"to {request.recipient}."
        ),
        metadata={
            "amount": request.amount,
            "currency": request.currency,
            "recipient": request.recipient,
            "intended_amount":
                request.intended_amount,
            "intended_recipient":
                request.intended_recipient,
            "category": request.category,
            "actual_spending_today":
                actual_spending_today,
        },
    )

    # ========================================================
    # 12. AUDIT — DECISION
    # ========================================================

    action_ledger.record(
        event_type="SECURITY_DECISION",
        agent_id=request.agent_id,
        session_id=request.session_id,
        intent_id=request.intent_id,
        message=(
            f"AgentShield decision: "
            f"{decision}."
        ),
        metadata={
            "decision": decision,
            "risk_score": risk_score,
            "intent_match": intent_match,
            "authorization_valid":
                authorization_valid,
            "actual_spending_today":
                actual_spending_today,
            "daily_limit":
                server_daily_limit,
            "max_transaction_amount":
                server_max_transaction_amount,
            "approval_threshold":
                server_approval_threshold,
            "new_daily_total":
                new_daily_total,
            "remaining_daily_limit":
                remaining_daily_limit,
            "agent_behavior_score":
                behavior_result.score,
            "agent_behavior_level":
                behavior_result.level,
            "repeated_attempts":
                behavior_result.repeated_attempts,
            "agent_action":
                behavior_result.action,
            "approval_id":
                approval_id,
        },
    )

    return RiskDecision(
        decision=decision,
        risk_score=risk_score,
        reasons=reasons,
        remaining_daily_limit=
            remaining_daily_limit,
        intent_match=intent_match,
        authorization_valid=
            authorization_valid,
        agent_behavior_score=
            behavior_result.score,
        agent_behavior_level=
            behavior_result.level,
        repeated_attempts=
            behavior_result.repeated_attempts,
        agent_action=
            behavior_result.action,
        approval_id=approval_id,
    )


# ============================================================
# DIRECT AGENT PAYMENT
# ============================================================

@app.post("/agent/create-payment")
def create_agent_payment(
    request: PaymentRequest,
    request_obj: Request,
):
    """
    Evaluate a payment and create a Razorpay order
    only when AgentShield returns ALLOW.

    Idempotency is deliberately checked BEFORE calling
    the behavior engine so that a retry does not count as
    another behavioral attempt.
    """

    # ========================================================
    # 1. IDEMPOTENCY KEY
    # ========================================================

    idempotency_key = (
        request_obj.headers.get(
            "Idempotency-Key"
        )
    )

    if not idempotency_key:

        idempotency_key = (
            f"{request.agent_id}:"
            f"{request.intent_id}"
        )

    # ========================================================
    # 2. REQUEST HASH
    # ========================================================

    request_hash = (
        idempotency_service.create_request_hash(
            request.model_dump()
        )
    )

    # ========================================================
    # 3. CHECK EXISTING REQUEST
    # ========================================================

    existing_request = (
        idempotency_service.get(
            idempotency_key
        )
    )

    if existing_request:

        existing_hash = (
            existing_request["request_hash"]
        )

        # ----------------------------------------------------
        # Same key + different request = security error
        # ----------------------------------------------------

        if existing_hash != request_hash:

            raise HTTPException(
                status_code=409,
                detail=(
                    "Idempotency-Key was already "
                    "used with a different payment "
                    "request."
                ),
            )

        existing_status = (
            existing_request["status"]
        )

        existing_order_id = (
            existing_request["order_id"]
        )

        # ----------------------------------------------------
        # Completed request
        # ----------------------------------------------------

        if (
            existing_status == "COMPLETED"
            and existing_order_id
        ):

            existing_order = (
                payment_ledger.get(
                    existing_order_id
                )
            )

            if existing_order:

                return {
                    "status":
                        "order_created",

                    "decision":
                        "ALLOW",

                    "order_id":
                        existing_order.order_id,

                    "amount":
                        existing_order.amount,

                    "currency":
                        existing_order.currency,

                    "policy_limits":
                        get_security_settings(),

                    "key_id":
                        os.getenv(
                            "RAZORPAY_KEY_ID"
                        ),

                    "idempotent_replay":
                        True,

                    "message": (
                        "Existing Razorpay order "
                        "returned for repeated "
                        "idempotent request."
                    ),
                }

        # ----------------------------------------------------
        # Processing request
        # ----------------------------------------------------

        if existing_status == "PROCESSING":

            raise HTTPException(
                status_code=409,
                detail=(
                    "This payment request is "
                    "already being processed."
                ),
            )

        # ----------------------------------------------------
        # Failed request
        # ----------------------------------------------------

        if existing_status == "FAILED":

            raise HTTPException(
                status_code=409,
                detail=(
                    "This idempotency key belongs "
                    "to a previously failed payment "
                    "request. Use a new key to retry."
                ),
            )

    # ========================================================
    # 4. NOW RUN AGENTSHIELD SECURITY
    # ========================================================

    decision = payment_request(
        request
    )

    # ========================================================
    # 5. REVIEW / BLOCK
    # ========================================================

    if decision.decision != "ALLOW":

        if decision.risk_score >= 70:
            risk_level = "HIGH"

        elif decision.risk_score >= 30:
            risk_level = "MEDIUM"

        else:
            risk_level = "LOW"

        return {
            "status":
                "payment_not_created",

            "decision":
                decision.decision,

            "risk_score":
                decision.risk_score,

            "risk_level":
                risk_level,

            "risk_reasons":
                decision.reasons,

            "approval_id":
                decision.approval_id,

            "agent_behavior_score":
                decision.agent_behavior_score,

            "agent_behavior_level":
                decision.agent_behavior_level,

            "repeated_attempts":
                decision.repeated_attempts,

            "message": (
                "Razorpay order was not "
                "created because AgentShield "
                "did not approve the payment."
            ),
        }

    # ========================================================
    # 6. RESERVE IDEMPOTENCY KEY
    # ========================================================

    reserved = (
        idempotency_service.reserve(
            idempotency_key=
                idempotency_key,

            agent_id=
                request.agent_id,

            intent_id=
                request.intent_id,

            request_hash=
                request_hash,
        )
    )

    if not reserved:

        # A concurrent request may have won the race.
        existing_request = (
            idempotency_service.get(
                idempotency_key
            )
        )

        if (
            existing_request
            and existing_request["status"]
            == "COMPLETED"
            and existing_request["order_id"]
        ):

            existing_order = (
                payment_ledger.get(
                    existing_request["order_id"]
                )
            )

            if existing_order:

                return {
                    "status":
                        "order_created",

                    "decision":
                        "ALLOW",

                    "order_id":
                        existing_order.order_id,

                    "amount":
                        existing_order.amount,

                    "currency":
                        existing_order.currency,

                    "policy_limits":
                        get_security_settings(),

                    "key_id":
                        os.getenv(
                            "RAZORPAY_KEY_ID"
                        ),

                    "idempotent_replay":
                        True,
                }

        raise HTTPException(
            status_code=409,
            detail=(
                "This payment request is "
                "already being processed."
            ),
        )

    # ========================================================
    # 7. CREATE RECEIPT
    # ========================================================

    receipt = (
        f"agent_"
        f"{uuid.uuid4().hex[:16]}"
    )

    # ========================================================
    # 8. CREATE RAZORPAY ORDER
    # ========================================================

    try:

        order = create_order(
            amount_inr=request.amount,
            receipt=receipt,
            notes={
                "agent_id":
                    request.agent_id,

                "session_id":
                    request.session_id,

                "intent_id":
                    request.intent_id,

                "recipient":
                    request.recipient,

                "category":
                    request.category,

                "agentshield_risk_score":
                    str(
                        decision.risk_score
                    ),

                "agent_behavior_score":
                    str(
                        decision.agent_behavior_score
                    ),
            },
        )

    except Exception as exc:

        idempotency_service.fail(
            idempotency_key
        )

        action_ledger.record(
            event_type=
                "RAZORPAY_ORDER_FAILED",
            agent_id=
                request.agent_id,
            session_id=
                request.session_id,
            intent_id=
                request.intent_id,
            message=
                "Razorpay order creation failed.",
            metadata={
                "error":
                    str(exc),

                "amount":
                    request.amount,

                "idempotency_key":
                    idempotency_key,
            },
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "AgentShield approved the "
                "payment, but Razorpay order "
                f"creation failed: {str(exc)}"
            ),
        )

    # ========================================================
    # 9. REGISTER PAYMENT ORDER
    # ========================================================

    try:

        payment_ledger.register_order(
            order_id=
                order["id"],

            agent_id=
                request.agent_id,

            session_id=
                request.session_id,

            intent_id=
                request.intent_id,

            amount=
                order["amount"],

            currency=
                order["currency"],
        )

    except Exception as exc:

        # The Razorpay order exists, so mark the
        # idempotency record as failed from the
        # application's point of view.
        idempotency_service.fail(
            idempotency_key
        )

        action_ledger.record(
            event_type=
                "PAYMENT_LEDGER_FAILED",

            agent_id=
                request.agent_id,

            session_id=
                request.session_id,

            intent_id=
                request.intent_id,

            message=(
                "Razorpay order was created, "
                "but AgentShield could not "
                "register it in the payment ledger."
            ),

            metadata={
                "order_id":
                    order["id"],

                "error":
                    str(exc),

                "idempotency_key":
                    idempotency_key,
            },
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Razorpay order was created but "
                "AgentShield failed to register "
                "the payment."
            ),
        )

    # ========================================================
    # 10. COMPLETE IDEMPOTENCY
    # ========================================================

    idempotency_service.complete(
        idempotency_key=
            idempotency_key,

        order_id=
            order["id"],
    )

    # ========================================================
    # 11. ACTION LEDGER
    # ========================================================

    action_ledger.record(
        event_type=
            "RAZORPAY_ORDER_CREATED",

        agent_id=
            request.agent_id,

        session_id=
            request.session_id,

        intent_id=
            request.intent_id,

        message=(
            "Razorpay order created after "
            "AgentShield approval."
        ),

        metadata={
            "order_id":
                order["id"],

            "amount":
                order["amount"],

            "currency":
                order["currency"],

            "receipt":
                order["receipt"],

            "idempotency_key":
                idempotency_key,
        },
    )

    # ========================================================
    # 12. RESPONSE
    # ========================================================

    return {
        "status":
            "order_created",

        "decision":
            "ALLOW",

        "risk_score":
            decision.risk_score,

        "risk_level": (
            "HIGH"
            if decision.risk_score >= 70
            else (
                "MEDIUM"
                if decision.risk_score >= 30
                else "LOW"
            )
        ),

        "agent_behavior_score":
            decision.agent_behavior_score,

        "agent_behavior_level":
            decision.agent_behavior_level,

        "repeated_attempts":
            decision.repeated_attempts,

        "order_id":
            order["id"],

        "amount":
            order["amount"],

        "currency":
            order["currency"],

        "policy_limits":
            get_security_settings(),

        "receipt":
            order["receipt"],

        "key_id":
            os.getenv(
                "RAZORPAY_KEY_ID"
            ),

        "idempotent_replay":
            False,
    }


# ============================================================
# RECURRING SUBSCRIPTION HELPERS
# ============================================================

def _create_recurring_subscription_from_request(
    *,
    name: str,
    recipient: str,
    amount: float,
    currency: str,
    period: str,
    interval: int,
    total_count: int,
    agent_id: str,
    session_id: str,
    intent_id: str,
    approval_id: str | None = None,
    category: str = "subscription",
    existing_request_id: str | None = None,
) -> dict:
    plan = create_plan(
        name=name,
        amount=amount,
        currency=currency,
        period=period,
        interval=interval,
        description=f"AgentShield recurring subscription for {recipient}",
        notes={
            "agent_id": agent_id,
            "intent_id": intent_id,
            "recipient": recipient,
        },
    )
    subscription = create_razorpay_subscription(
        plan_id=plan["id"],
        total_count=total_count,
        quantity=1,
        notes={
            "agent_id": agent_id,
            "session_id": session_id,
            "intent_id": intent_id,
            "recipient": recipient,
            "approval_id": approval_id or "",
        },
    )
    if existing_request_id:
        update_subscription(
            existing_request_id,
            status=subscription.get("status", "created"),
            plan_id=plan["id"],
            razorpay_subscription_id=subscription["id"],
            last_error=None,
        )
        subscription_request_id = existing_request_id
    else:
        local = create_local_record(
            approval_id=approval_id,
            intent_id=intent_id,
            agent_id=agent_id,
            session_id=session_id,
            name=name,
            recipient=recipient,
            amount=amount,
            currency=currency,
            period=period,
            interval=interval,
            total_count=total_count,
            plan_id=plan["id"],
            razorpay_subscription_id=subscription["id"],
            status=subscription.get("status", "created"),
            request_payload={
                "category": category,
            },
        )
        subscription_request_id = local["subscription_request_id"]
    return {
        "subscription_request_id": subscription_request_id,
        "subscription_id": subscription["id"],
        "plan_id": plan["id"],
        "status": subscription.get("status", "created"),
        "short_url": subscription.get("short_url"),
        "key_id": os.getenv("RAZORPAY_KEY_ID"),
    }


def _process_recurring_subscription_request(request: RecurringSubscriptionRequest) -> dict:
    settings = get_security_settings()
    intent_id = request.intent_id or f"SUB-INTENT-{uuid.uuid4().hex[:12]}"
    payment_request = PaymentRequest(
        intent_id=intent_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        recipient=request.recipient,
        amount=request.amount,
        currency=request.currency,
        intended_amount=request.intended_amount or request.amount,
        intended_recipient=request.intended_recipient or request.recipient,
        max_transaction_amount=settings["max_transaction_amount"],
        daily_limit=settings["daily_limit"],
        amount_spent_today=0.0,
        previous_payment_same_request=False,
        merchant=request.recipient,
        category=request.category,
        merchant_known=request.merchant_known,
        unusual=request.unusual,
    )
    decision = payment_request(payment_request)
    if decision.decision == "BLOCK":
        return {
            "status": "blocked",
            "decision": "BLOCK",
            "intent_id": intent_id,
            "risk_score": decision.risk_score,
            "reasons": decision.reasons,
            "message": "AgentShield blocked the recurring subscription request.",
        }

    if decision.decision == "REVIEW":
        local = create_local_record(
            approval_id=decision.approval_id,
            intent_id=intent_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            name=request.name,
            recipient=request.recipient,
            amount=request.amount,
            currency=request.currency,
            period=request.period,
            interval=request.interval,
            total_count=request.total_count,
            plan_id=None,
            razorpay_subscription_id=None,
            status="AWAITING_APPROVAL",
            request_payload=request.model_dump(),
        )
        return {
            "status": "awaiting_approval",
            "decision": "REVIEW",
            "approval_id": decision.approval_id,
            "subscription_request_id": local["subscription_request_id"],
            "intent_id": intent_id,
            "risk_score": decision.risk_score,
            "reasons": decision.reasons,
            "message": "AgentShield paused the recurring subscription for human approval.",
        }

    try:
        created = _create_recurring_subscription_from_request(
            name=request.name,
            recipient=request.recipient,
            amount=request.amount,
            currency=request.currency,
            period=request.period,
            interval=request.interval,
            total_count=request.total_count,
            agent_id=request.agent_id,
            session_id=request.session_id,
            intent_id=intent_id,
            category=request.category,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to create Razorpay subscription: {exc}")

    action_ledger.record(
        event_type="RAZORPAY_SUBSCRIPTION_CREATED",
        agent_id=request.agent_id,
        session_id=request.session_id,
        intent_id=intent_id,
        message="Recurring subscription created after AgentShield approval.",
        metadata=created,
    )
    return {
        "status": "subscription_created",
        "decision": "ALLOW",
        "source": "AI_PAYMENT_AGENT",
        "risk_score": decision.risk_score,
        "reasons": decision.reasons,
        "intent_id": intent_id,
        **created,
        "message": "Recurring subscription created. Complete Razorpay authorization to activate it.",
    }


# ============================================================
# RECURRING SUBSCRIPTIONS
# ============================================================

@app.get("/subscriptions")
def get_subscriptions():
    return {"items": list_subscriptions()}


@app.post("/subscriptions/create")
def create_subscription_endpoint(request: RecurringSubscriptionRequest):
    return _process_recurring_subscription_request(request)


@app.get("/subscriptions/{subscription_request_id}")
def get_subscription_endpoint(subscription_request_id: str):
    item = get_subscription(subscription_request_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Recurring subscription not found.")
    return item


@app.post("/subscriptions/{subscription_request_id}/cancel")
def cancel_subscription_endpoint(subscription_request_id: str):
    item = get_subscription(subscription_request_id)
    if item is None or not item.get("razorpay_subscription_id"):
        raise HTTPException(status_code=404, detail="Recurring subscription not found.")
    try:
        response = get_razorpay_client().subscription.cancel(
            item["razorpay_subscription_id"],
            {"cancel_at_cycle_end": False},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to cancel Razorpay subscription: {exc}")
    updated = update_subscription(
        subscription_request_id,
        status=response.get("status", "cancelled"),
        cancelled_at=_utc_now_iso(),
        last_error=None,
    )
    return {"status": "cancelled", "subscription": updated}


@app.post("/razorpay/verify-subscription")
def verify_subscription(request: SubscriptionVerificationRequest):
    item = get_subscription(request.subscription_request_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Recurring subscription not found.")
    if item.get("razorpay_subscription_id") != request.razorpay_subscription_id:
        raise HTTPException(status_code=400, detail="Subscription ID does not match AgentShield record.")
    try:
        valid = verify_subscription_signature(
            razorpay_payment_id=request.razorpay_payment_id,
            razorpay_subscription_id=request.razorpay_subscription_id,
            razorpay_signature=request.razorpay_signature,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not valid:
        raise HTTPException(status_code=400, detail="Invalid Razorpay subscription signature.")

    updated = update_subscription(
        request.subscription_request_id,
        status="AUTHENTICATED",
        authorization_payment_id=request.razorpay_payment_id,
        activated_at=_utc_now_iso(),
        last_error=None,
    )
    action_ledger.record(
        event_type="RAZORPAY_SUBSCRIPTION_AUTHENTICATED",
        agent_id=item["agent_id"],
        session_id=item["session_id"],
        intent_id=item["intent_id"],
        message="Recurring subscription authorization signature verified.",
        metadata={
            "subscription_request_id": request.subscription_request_id,
            "subscription_id": request.razorpay_subscription_id,
            "payment_id": request.razorpay_payment_id,
        },
    )
    return {
        "status": "verified",
        "decision": "ALLOW",
        "subscription": updated,
    }


# ============================================================
# AI PAYMENT AGENT COMMAND
# ============================================================

class _CommandRequestAdapter:
    """Minimal request adapter for the existing payment pipeline."""

    def __init__(self, idempotency_key: str):
        self.headers = {"Idempotency-Key": idempotency_key}


@app.post("/agent/command")
def agent_command(request: AgentCommandRequest):
    """Process a natural-language payment or bill reminder command."""
    command = request.command.strip()
    if not command:
        raise HTTPException(status_code=400, detail="Command cannot be empty.")

    try:
        parsed = parse_agent_command(command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    agent_id = request.agent_id.strip() or "CHAT-AGENT-001"
    session_id = (
        request.session_id.strip()
        if request.session_id
        else f"CHAT-SESSION-{uuid.uuid4().hex[:12]}"
    )
    action = parsed["action"]
    schedule_type = parsed["schedule_type"]

    if action == "BILL_REMINDER":
        bill = create_recurring_bill(
            name=parsed["recipient"],
            recipient=parsed["recipient"],
            amount=parsed["amount"],
            currency=parsed["currency"],
            day_of_month=parsed.get("day_of_month"),
            reminder_days_before=3,
        )
        action_ledger.record(
            event_type="AI_BILL_REMINDER_CREATED",
            agent_id=agent_id,
            session_id=session_id,
            intent_id=bill["bill_id"],
            message="Recurring bill reminder created by AI Payment Agent.",
            metadata={
                "bill_id": bill["bill_id"],
                "recipient": parsed["recipient"],
                "amount": parsed["amount"],
                "currency": parsed["currency"],
                "schedule_type": schedule_type,
            },
        )
        return {
            "status": "reminder_created",
            "decision": "REMINDER",
            "source": "AI_PAYMENT_AGENT",
            "bill_id": bill["bill_id"],
            "agent_id": agent_id,
            "session_id": session_id,
            "parsed_command": parsed,
            "message": f"Monthly bill reminder created for {parsed['recipient']}.",
        }

    if action == "PAYMENT" and schedule_type in {"ONE_TIME", "MONTHLY"}:
        intent_id = f"CHAT-INTENT-{uuid.uuid4().hex[:12]}"

        if schedule_type == "MONTHLY":
            subscription_request = RecurringSubscriptionRequest(
                name=parsed["recipient"],
                recipient=parsed["recipient"],
                amount=parsed["amount"],
                currency=parsed.get("currency", "INR"),
                period="monthly",
                interval=1,
                total_count=12,
                agent_id=agent_id,
                session_id=session_id,
                intent_id=intent_id,
                intended_amount=(
                    request.intended_amount
                    if request.intended_amount is not None
                    else parsed["amount"]
                ),
                intended_recipient=(
                    request.intended_recipient
                    if request.intended_recipient
                    else parsed["recipient"]
                ),
                category=request.category or "subscription",
                merchant_known=request.merchant_known,
                unusual=request.unusual,
            )
            result = _process_recurring_subscription_request(subscription_request)
            result["parsed_command"] = parsed
            return result

        settings = get_security_settings()

        scheduled_at = parsed["scheduled_at"]
        if isinstance(scheduled_at, str):
            try:
                scheduled_at = datetime.fromisoformat(
                    scheduled_at
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Invalid scheduled payment time: "
                        f"{scheduled_at}"
                    ),
                ) from exc

        schedule = create_scheduled_payment(
            intent_id=intent_id,
            agent_id=agent_id,
            session_id=session_id,
            recipient=parsed["recipient"],
            amount=parsed["amount"],
            currency=parsed["currency"],
            intended_amount=(
                request.intended_amount
                if request.intended_amount is not None
                else parsed["amount"]
            ),
            intended_recipient=(
                request.intended_recipient
                if request.intended_recipient
                else parsed["recipient"]
            ),
            category=request.category,
            scheduled_at=scheduled_at,
            status="SCHEDULED",
            decision="SCHEDULED",
            approval_id=None,
            reasons=[],
            request_payload={
                "command": command,
                "parsed_command": parsed,
                "merchant": request.merchant or parsed["recipient"],
                "category": request.category,
                "merchant_known": request.merchant_known,
                "unusual": request.unusual,
                "max_transaction_amount": settings["max_transaction_amount"],
                "daily_limit": settings["daily_limit"],
                "approval_threshold": settings["approval_threshold"],
            },
            timezone_name=parsed["timezone"],
        )
        action_ledger.record(
            event_type="AI_PAYMENT_SCHEDULED",
            agent_id=agent_id,
            session_id=session_id,
            intent_id=intent_id,
            message=(
                "Payment scheduled through AI Payment Agent. "
                "Security will be re-evaluated at execution time."
            ),
            metadata={
                "schedule_id": schedule["schedule_id"],
                "scheduled_at": parsed["scheduled_at"],
                "amount": parsed["amount"],
                "recipient": parsed["recipient"],
            },
        )
        return {
            "status": "scheduled",
            "decision": "SCHEDULED",
            "source": "AI_PAYMENT_AGENT",
            "schedule_id": schedule["schedule_id"],
            "intent_id": intent_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "scheduled_at": parsed["scheduled_at"],
            "parsed_command": parsed,
            "message": (
                "Payment scheduled successfully. "
                "AgentShield will perform a fresh security check "
                "before creating the Razorpay order."
            ),
        }

    if action == "PAYMENT":
        intent_id = f"CHAT-INTENT-{uuid.uuid4().hex[:12]}"
        settings = get_security_settings()
        payment_request = PaymentRequest(
            intent_id=intent_id,
            agent_id=agent_id,
            session_id=session_id,
            recipient=parsed["recipient"],
            amount=parsed["amount"],
            currency=parsed["currency"],
            intended_amount=(
                request.intended_amount
                if request.intended_amount is not None
                else parsed["amount"]
            ),
            intended_recipient=(
                request.intended_recipient
                if request.intended_recipient
                else parsed["recipient"]
            ),
            max_transaction_amount=settings["max_transaction_amount"],
            daily_limit=settings["daily_limit"],
            amount_spent_today=0.0,
            previous_payment_same_request=False,
            merchant=request.merchant or parsed["recipient"],
            category=request.category,
            merchant_known=request.merchant_known,
            unusual=request.unusual,
        )
        result = create_agent_payment(
            payment_request,
            _CommandRequestAdapter(f"CHAT-PAYMENT-{intent_id}"),
        )
        result["parsed_command"] = parsed
        result["agent_id"] = agent_id
        result["session_id"] = session_id
        return result

    raise HTTPException(status_code=400, detail="Unsupported AI payment command.")


# ============================================================
# SCHEDULED PAYMENTS / RECURRING BILLS
# ============================================================

@app.get("/scheduled-payments")
def get_scheduled_payments(agent_id: str | None = None):
    items = list_scheduled_payments()
    if agent_id:
        items = [
            item for item in items
            if item.get("agent_id") == agent_id
        ]
    return {"items": items}


@app.get("/recurring-bills")
def get_recurring_bills():
    return {"items": list_recurring_bills()}


# ============================================================
# PAYMENT VERIFICATION
# ============================================================

@app.post(
    "/razorpay/verify-payment"
)
def verify_payment(
    request:
        PaymentVerificationRequest,
):
    """
    Verify Razorpay signature and ensure the payment
    belongs to an AgentShield-created transaction.
    """

    # ========================================================
    # 1. FIND AGENTSHIELD RECORD
    # ========================================================

    record = payment_ledger.get(
        request.razorpay_order_id
    )

    if record is None:

        action_ledger.record(
            event_type=
                "PAYMENT_REJECTED",

            agent_id=
                "UNKNOWN",

            session_id=
                "UNKNOWN",

            intent_id=
                "UNKNOWN",

            message=(
                "Payment rejected because the "
                "Razorpay order is not registered "
                "with AgentShield."
            ),

            metadata={
                "order_id":
                    request.razorpay_order_id,

                "payment_id":
                    request.razorpay_payment_id,
            },
        )

        raise HTTPException(
            status_code=403,
            detail=(
                "This Razorpay order is not "
                "registered with AgentShield."
            ),
        )

    # ========================================================
    # 2. APPROVAL VALIDATION
    # ========================================================

    if record.approval_id:

        approval = approval_service.get(
            record.approval_id
        )

        if approval is None:

            raise HTTPException(
                status_code=403,
                detail=(
                    "AgentShield approval "
                    "record could not be found."
                ),
            )

        if approval.status != "APPROVED":

            raise HTTPException(
                status_code=403,
                detail=(
                    "The AgentShield approval "
                    "is not currently valid."
                ),
            )

    # ========================================================
    # 3. SIGNATURE
    # ========================================================

    try:

        verify_payment_signature(
            order_id=
                request.razorpay_order_id,

            payment_id=
                request.razorpay_payment_id,

            signature=
                request.razorpay_signature,
        )

    except Exception:

        action_ledger.record(
            event_type=
                "PAYMENT_VERIFICATION_FAILED",

            agent_id=
                record.agent_id
                or "UNKNOWN",

            session_id=
                record.session_id
                or "UNKNOWN",

            intent_id=
                record.intent_id
                or "UNKNOWN",

            message=(
                "Razorpay payment signature "
                "verification failed."
            ),

            metadata={
                "order_id":
                    request.razorpay_order_id,

                "payment_id":
                    request.razorpay_payment_id,

                "approval_id":
                    record.approval_id,
            },
        )

        raise HTTPException(
            status_code=400,
            detail=(
                "Payment signature "
                "verification failed."
            ),
        )

    # ========================================================
    # 4. FETCH ORDER
    # ========================================================

    try:

        client = get_razorpay_client()

        razorpay_order = (
            client.order.fetch(
                request.razorpay_order_id
            )
        )

    except Exception as exc:

        raise HTTPException(
            status_code=502,
            detail=(
                "Unable to retrieve the "
                "Razorpay order for security "
                f"validation: {str(exc)}"
            ),
        )

    # ========================================================
    # 5. AMOUNT MATCH
    # ========================================================

    razorpay_amount = (
        razorpay_order.get(
            "amount"
        )
    )

    if (
        razorpay_amount is not None
        and record.amount is not None
        and int(razorpay_amount)
        != int(record.amount)
    ):

        action_ledger.record(
            event_type=
                "PAYMENT_REJECTED",

            agent_id=
                record.agent_id
                or "UNKNOWN",

            session_id=
                record.session_id
                or "UNKNOWN",

            intent_id=
                record.intent_id
                or "UNKNOWN",

            message=(
                "Payment rejected because the "
                "Razorpay amount does not match "
                "the AgentShield-authorized amount."
            ),

            metadata={
                "order_id":
                    request.razorpay_order_id,

                "authorized_amount":
                    record.amount,

                "razorpay_amount":
                    razorpay_amount,
            },
        )

        raise HTTPException(
            status_code=403,
            detail=(
                "Razorpay amount does not "
                "match the AgentShield record."
            ),
        )

    # ========================================================
    # 6. RAZORPAY NOTES MATCH
    # ========================================================

    notes = (
        razorpay_order.get(
            "notes",
            {},
        )
        or {}
    )

    if (
        record.agent_id
        and notes.get("agent_id")
        and notes.get("agent_id")
        != record.agent_id
    ):

        raise HTTPException(
            status_code=403,
            detail=(
                "Agent identity mismatch "
                "between Razorpay and AgentShield."
            ),
        )

    if (
        record.intent_id
        and notes.get("intent_id")
        and notes.get("intent_id")
        != record.intent_id
    ):

        raise HTTPException(
            status_code=403,
            detail=(
                "Intent mismatch between "
                "Razorpay and AgentShield."
            ),
        )

    if (
        record.approval_id
        and notes.get("approval_id")
        and notes.get("approval_id")
        != record.approval_id
    ):

        raise HTTPException(
            status_code=403,
            detail=(
                "Approval mismatch between "
                "Razorpay and AgentShield."
            ),
        )

    # ========================================================
    # 7. SAVE VERIFIED STATE
    # ========================================================

    payment_ledger.add_event(
        event_id=(
            "verify:"
            f"{request.razorpay_payment_id}"
        ),

        order_id=
            request.razorpay_order_id,

        payment_id=
            request.razorpay_payment_id,

        status=
            "SIGNATURE_VERIFIED",

        amount=
            record.amount,

        currency=
            record.currency,

        event=
            "checkout.signature_verified",
    )

    # ========================================================
    # 8. AUDIT
    # ========================================================

    action_ledger.record(
        event_type=
            "PAYMENT_VERIFIED",

        agent_id=
            record.agent_id
            or "UNKNOWN",

        session_id=
            record.session_id
            or "UNKNOWN",

        intent_id=
            record.intent_id
            or "UNKNOWN",

        message=(
            "Razorpay payment verified and "
            "matched to the AgentShield transaction."
        ),

        metadata={
            "order_id":
                request.razorpay_order_id,

            "payment_id":
                request.razorpay_payment_id,

            "approval_id":
                record.approval_id,

            "amount":
                record.amount,

            "agent_id":
                record.agent_id,

            "intent_id":
                record.intent_id,
        },
    )

    return {
        "status":
            "verified",

        "message": (
            "Payment verified and matched "
            "to the AgentShield transaction."
        ),

        "order_id":
            request.razorpay_order_id,

        "payment_id":
            request.razorpay_payment_id,

        "agent_id":
            record.agent_id,

        "intent_id":
            record.intent_id,

        "approval_id":
            record.approval_id,
    }


# ============================================================
# PENDING APPROVALS
# ============================================================

@app.get(
    "/approvals/pending"
)
def get_pending_approvals():

    requests = (
        approval_service.list_pending()
    )

    return {
        "count":
            len(requests),

        "requests": [

            {
                "approval_id":
                    item.approval_id,

                "intent_id":
                    item.intent_id,

                "agent_id":
                    item.agent_id,

                "session_id":
                    item.session_id,

                "recipient":
                    item.recipient,

                "amount":
                    item.amount,

                "currency":
                    item.currency,

                "risk_score":
                    item.risk_score,

                "risk_level":
                    item.risk_level,

                "reasons":
                    item.reasons,

                "status":
                    item.status,

                "created_at":
                    item.created_at,

                "approved_at":
                    item.approved_at,
            }

            for item in requests
        ],
    }


# ============================================================
# GET APPROVAL
# ============================================================

@app.get(
    "/approvals/{approval_id}"
)
def get_approval(
    approval_id: str,
):

    approval = approval_service.get(
        approval_id
    )

    if approval is None:

        raise HTTPException(
            status_code=404,
            detail=
                "Approval request not found.",
        )

    return {
        "approval_id":
            approval.approval_id,

        "intent_id":
            approval.intent_id,

        "agent_id":
            approval.agent_id,

        "session_id":
            approval.session_id,

        "recipient":
            approval.recipient,

        "amount":
            approval.amount,

        "currency":
            approval.currency,

        "risk_score":
            approval.risk_score,

        "risk_level":
            approval.risk_level,

        "reasons":
            approval.reasons,

        "status":
            approval.status,

        "created_at":
            approval.created_at,

        "approved_at":
            approval.approved_at,
    }


# ============================================================
# APPROVE
# ============================================================

@app.post(
    "/approvals/{approval_id}/approve"
)
def approve_payment(
    approval_id: str,
):

    try:

        approval = (
            approval_service.approve(
                approval_id
            )
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc),
        )

    scheduled_items = list_scheduled_payments()
    for scheduled in scheduled_items:
        if (
            scheduled.get("intent_id") == approval.intent_id
            and scheduled.get("approval_id") == approval.approval_id
            and scheduled.get("status") == "AWAITING_APPROVAL"
        ):
            update_scheduled_payment(
                scheduled["schedule_id"],
                status="APPROVED",
                decision="ALLOW",
                approval_id=approval.approval_id,
                last_error=None,
            )
            break

    recurring = get_by_approval_id(approval.approval_id)
    recurring_result = None
    if recurring is not None and recurring.get("status") == "AWAITING_APPROVAL":
        try:
            recurring_result = _create_recurring_subscription_from_request(
                name=recurring["name"],
                recipient=recurring["recipient"],
                amount=float(recurring["amount"]),
                currency=recurring["currency"],
                period=recurring["period"],
                interval=int(recurring["interval"]),
                total_count=int(recurring["total_count"]),
                agent_id=recurring["agent_id"],
                session_id=recurring["session_id"],
                intent_id=recurring["intent_id"],
                approval_id=approval.approval_id,
                category=(recurring.get("request_payload") or {}).get("category", "subscription"),
                existing_request_id=recurring["subscription_request_id"],
            )
            update_subscription(
                recurring["subscription_request_id"],
                status="created",
                plan_id=recurring_result["plan_id"],
                razorpay_subscription_id=recurring_result["subscription_id"],
                last_error=None,
            )
        except Exception as exc:
            update_subscription(
                recurring["subscription_request_id"],
                last_error=str(exc),
            )
            raise HTTPException(
                status_code=502,
                detail=f"Human approval succeeded, but recurring subscription creation failed: {exc}",
            )

    action_ledger.record(
        event_type=
            "HUMAN_APPROVAL",

        agent_id=
            approval.agent_id,

        session_id=
            approval.session_id,

        intent_id=
            approval.intent_id,

        message=(
            "Human approved this payment "
            "request."
        ),

        metadata={
            "approval_id":
                approval.approval_id,

            "amount":
                approval.amount,

            "recipient":
                approval.recipient,

            "status":
                approval.status,
        },
    )

    response = {
        "status": "approved",
        "approval_id": approval.approval_id,
        "message": (
            "Human approval recorded. This payment may now proceed."
        ),
    }
    if recurring_result is not None:
        response.update({
            "subscription": recurring_result,
            "message": "Human approval recorded. Complete Razorpay subscription authorization to activate the recurring payment.",
        })
    return response


# ============================================================
# REJECT
# ============================================================

@app.post(
    "/approvals/{approval_id}/reject"
)
def reject_payment(
    approval_id: str,
):

    try:

        approval = (
            approval_service.reject(
                approval_id
            )
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc),
        )

    action_ledger.record(
        event_type=
            "HUMAN_REJECTION",

        agent_id=
            approval.agent_id,

        session_id=
            approval.session_id,

        intent_id=
            approval.intent_id,

        message=(
            "Human rejected this payment "
            "request."
        ),

        metadata={
            "approval_id":
                approval.approval_id,

            "amount":
                approval.amount,

            "recipient":
                approval.recipient,

            "status":
                approval.status,
        },
    )

    return {
        "status":
            "rejected",

        "approval_id":
            approval.approval_id,

        "message":
            "Payment request rejected.",
    }


# ============================================================
# CREATE PAYMENT AFTER HUMAN APPROVAL
# ============================================================

@app.post(
    "/approvals/{approval_id}/create-payment"
)
def create_approved_payment(
    approval_id: str,
):
    """
    Create a Razorpay order only after human approval.
    """

    approval = approval_service.get(
        approval_id
    )

    if approval is None:

        raise HTTPException(
            status_code=404,
            detail=
                "Approval request not found.",
        )

    # ========================================================
    # 1. MUST BE APPROVED
    # ========================================================

    if approval.status != "APPROVED":

        return {
            "status":
                "payment_not_created",

            "decision":
                "BLOCK",

            "approval_id":
                approval_id,

            "message": (
                "This payment does not "
                "have human approval."
            ),
        }

    # ========================================================
    # 2. ONE-TIME APPROVAL
    # ========================================================

    existing_payment = (
        payment_ledger.find_by_approval(
            approval_id
        )
    )

    if existing_payment is not None:

        raise HTTPException(
            status_code=409,
            detail=(
                "This approval has already "
                "been used to create a "
                "payment order."
            ),
        )

    # ========================================================
    # 3. RECEIPT
    # ========================================================

    receipt = (
        f"approval_"
        f"{uuid.uuid4().hex[:16]}"
    )

    # ========================================================
    # 4. CREATE ORDER
    # ========================================================

    try:

        order = create_order(
            amount_inr=
                approval.amount,

            receipt=
                receipt,

            notes={
                "approval_id":
                    approval.approval_id,

                "intent_id":
                    approval.intent_id,

                "agent_id":
                    approval.agent_id,

                "session_id":
                    approval.session_id,

                "recipient":
                    approval.recipient,

                "agentshield_risk_score":
                    str(
                        approval.risk_score
                    ),
            },
        )

    except Exception as exc:

        action_ledger.record(
            event_type=
                "RAZORPAY_ORDER_FAILED",

            agent_id=
                approval.agent_id,

            session_id=
                approval.session_id,

            intent_id=
                approval.intent_id,

            message=(
                "Razorpay order creation "
                "failed after human approval."
            ),

            metadata={
                "approval_id":
                    approval.approval_id,

                "amount":
                    approval.amount,

                "error":
                    str(exc),
            },
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Human approval succeeded, "
                "but Razorpay order creation "
                f"failed: {str(exc)}"
            ),
        )

    # ========================================================
    # 5. REGISTER ORDER
    # ========================================================

    payment_ledger.register_order(
        order_id=
            order["id"],

        agent_id=
            approval.agent_id,

        session_id=
            approval.session_id,

        intent_id=
            approval.intent_id,

        approval_id=
            approval.approval_id,

        amount=
            order["amount"],

        currency=
            order["currency"],
    )

    # ========================================================
    # 6. AUDIT
    # ========================================================

    action_ledger.record(
        event_type=
            "RAZORPAY_ORDER_CREATED",

        agent_id=
            approval.agent_id,

        session_id=
            approval.session_id,

        intent_id=
            approval.intent_id,

        message=(
            "Razorpay order created after "
            "human approval."
        ),

        metadata={
            "approval_id":
                approval.approval_id,

            "order_id":
                order["id"],

            "amount":
                order["amount"],

            "currency":
                order["currency"],

            "receipt":
                order["receipt"],
        },
    )

    scheduled_items = list_scheduled_payments()
    for scheduled in scheduled_items:
        if (
            scheduled.get("approval_id") == approval.approval_id
            and scheduled.get("status") in {
                "AWAITING_APPROVAL",
                "APPROVED",
                "EXECUTING",
            }
        ):
            update_scheduled_payment(
                scheduled["schedule_id"],
                status="COMPLETED",
                decision="ALLOW",
                order_id=order["id"],
                executed_at=_utc_now_iso(),
                last_error=None,
            )
            break

    return {
        "status":
            "order_created",

        "decision":
            "ALLOW",

        "approval_id":
            approval.approval_id,

        "order_id":
            order["id"],

        "amount":
            order["amount"],

        "currency":
            order["currency"],

        "receipt":
            order["receipt"],

        "key_id":
            os.getenv(
                "RAZORPAY_KEY_ID"
            ),
    }


# ============================================================
# RAZORPAY WEBHOOK
# ============================================================

@app.post(
    "/webhooks/razorpay"
)
async def razorpay_webhook(
    request: Request,
):
    """
    Verify and process Razorpay webhook events.
    """

    if not RAZORPAY_WEBHOOK_SECRET:

        raise HTTPException(
            status_code=500,
            detail=(
                "RAZORPAY_WEBHOOK_SECRET "
                "is not configured."
            ),
        )

    # ========================================================
    # RAW BODY
    # ========================================================

    raw_body = await request.body()

    # ========================================================
    # SIGNATURE
    # ========================================================

    signature = request.headers.get(
        "X-Razorpay-Signature"
    )

    if not signature:

        raise HTTPException(
            status_code=400,
            detail=(
                "Missing "
                "X-Razorpay-Signature header."
            ),
        )

    # ========================================================
    # VERIFY WEBHOOK
    # ========================================================

    try:

        verify_webhook_signature(
            payload=
                raw_body.decode(
                    "utf-8"
                ),

            signature=
                signature,

            webhook_secret=
                RAZORPAY_WEBHOOK_SECRET,
        )

    except Exception:

        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid Razorpay "
                "webhook signature."
            ),
        )

    # ========================================================
    # JSON
    # ========================================================

    try:

        payload = await request.json()

    except Exception:

        raise HTTPException(
            status_code=400,
            detail=
                "Invalid webhook JSON.",
        )

    event_name = payload.get(
        "event",
        "unknown",
    )

    # ========================================================
    # EVENT ID
    # ========================================================

    event_id = (
        request.headers.get(
            "x-razorpay-event-id"
        )
        or (
            f"{event_name}:"
            f"{hash(raw_body)}"
        )
    )

    # ========================================================
    # PAYMENT ENTITY
    # ========================================================

    payment_entity = (
        payload
        .get(
            "payload",
            {},
        )
        .get(
            "payment",
            {},
        )
        .get(
            "entity",
            {},
        )
    )

    order_id = payment_entity.get(
        "order_id"
    )

    payment_id = payment_entity.get(
        "id"
    )

    amount = payment_entity.get(
        "amount"
    )

    currency = payment_entity.get(
        "currency"
    )

    # ========================================================
    # STATUS
    # ========================================================

    if event_name == "payment.captured":

        status = "CAPTURED"

    elif event_name == "payment.authorized":

        status = "AUTHORIZED"

    elif event_name == "payment.failed":

        status = "FAILED"

    elif event_name == "order.paid":

        status = "PAID"

    else:

        status = event_name.upper()

    # ========================================================
    # PAYMENT LEDGER
    # ========================================================

    processed = True

    if order_id:

        processed = (
            payment_ledger.add_event(
                event_id=
                    event_id,

                order_id=
                    order_id,

                payment_id=
                    payment_id,

                status=
                    status,

                amount=
                    amount,

                currency=
                    currency,

                event=
                    event_name,
            )
        )

    # ========================================================
    # CORRELATE
    # ========================================================

    record = None

    if order_id:

        record = payment_ledger.get(
            order_id
        )

    # ========================================================
    # AUDIT
    # ========================================================

    action_ledger.record(
        event_type=
            "RAZORPAY_WEBHOOK",

        agent_id=(
            record.agent_id
            if record
            else "UNKNOWN"
        ),

        session_id=(
            record.session_id
            if record
            else "UNKNOWN"
        ),

        intent_id=(
            record.intent_id
            if record
            else "UNKNOWN"
        ),

        message=(
            f"Razorpay webhook received: "
            f"{event_name}."
        ),

        metadata={
            "event_id":
                event_id,

            "order_id":
                order_id,

            "payment_id":
                payment_id,

            "amount":
                amount,

            "currency":
                currency,

            "status":
                status,

            "processed":
                processed,

            "approval_id": (
                record.approval_id
                if record
                else None
            ),
        },
    )

    return {
        "status":
            "accepted",

        "event":
            event_name,

        "processed":
            processed,

        "order_id":
            order_id,

        "payment_id":
            payment_id,
    }


# ============================================================
# PAYMENT STATUS
# ============================================================

@app.get(
    "/payments/{order_id}"
)
def get_payment_status(
    order_id: str,
):

    record = payment_ledger.get(
        order_id
    )

    if record is None:

        return {
            "status":
                "NOT_FOUND",

            "order_id":
                order_id,
        }

    return {
        "order_id":
            record.order_id,

        "payment_id":
            record.payment_id,

        "status":
            record.status,

        "amount":
            record.amount,

        "currency":
            record.currency,

        "event":
            record.event,

        "received_at":
            record.received_at,

        "agent_id":
            record.agent_id,

        "session_id":
            record.session_id,

        "intent_id":
            record.intent_id,

        "approval_id":
            record.approval_id,
    }


# ============================================================
# ALL PAYMENTS
# ============================================================

@app.get("/payments")
def list_payments():

    records = (
        payment_ledger.list_all()
    )

    return {
        "count":
            len(records),

        "payments": [

            {
                "order_id":
                    record.order_id,

                "payment_id":
                    record.payment_id,

                "status":
                    record.status,

                "amount":
                    record.amount,

                "currency":
                    record.currency,

                "event":
                    record.event,

                "received_at":
                    record.received_at,

                "agent_id":
                    record.agent_id,

                "session_id":
                    record.session_id,

                "intent_id":
                    record.intent_id,

                "approval_id":
                    record.approval_id,
            }

            for record in records
        ],
    }


# ============================================================
# DAILY SPENDING
# ============================================================

@app.get(
    "/agent/{agent_id}/daily-spending"
)
def get_daily_spending(
    agent_id: str,
):

    spending_paise = (
        payment_ledger.get_daily_spending(
            agent_id
        )
    )

    return {
        "agent_id":
            agent_id,

        "spending_paise":
            spending_paise,

        "spending_inr":
            spending_paise / 100,
    }


# ============================================================
# BEHAVIOR HISTORY
# ============================================================

@app.get(
    "/agent/{agent_id}/session/{session_id}/behavior"
)
def get_behavior(
    agent_id: str,
    session_id: str,
):

    events = (
        behavior_engine.get_session_events(
            agent_id=
                agent_id,

            session_id=
                session_id,
        )
    )

    return {
        "agent_id":
            agent_id,

        "session_id":
            session_id,

        "event_count":
            len(events),

        "events":
            events,
    }


# ============================================================
# CLEAR BEHAVIOR
# ============================================================

@app.delete(
    "/agent/{agent_id}/session/{session_id}/behavior"
)
def clear_behavior(
    agent_id: str,
    session_id: str,
):

    behavior_engine.clear_session(
        agent_id=
            agent_id,

        session_id=
            session_id,
    )

    return {
        "status":
            "cleared",

        "agent_id":
            agent_id,

        "session_id":
            session_id,
    }


# ============================================================
# ACTION LEDGER
# ============================================================

@app.get("/ledger")
def get_ledger():

    events = (
        action_ledger.list_all()
    )

    return {
        "count":
            len(events),

        "events":
            events,
    }


# ============================================================
# SESSION LEDGER
# ============================================================

@app.get(
    "/ledger/agent/{agent_id}/session/{session_id}"
)
def get_session_ledger(
    agent_id: str,
    session_id: str,
):

    events = (
        action_ledger.list_session(
            agent_id=
                agent_id,

            session_id=
                session_id,
        )
    )

    return {
        "agent_id":
            agent_id,

        "session_id":
            session_id,

        "count":
            len(events),

        "events":
            events,
    }


# ============================================================
# INTENT LEDGER
# ============================================================

@app.get(
    "/ledger/intent/{intent_id}"
)
def get_intent_ledger(
    intent_id: str,
):

    events = (
        action_ledger.list_intent(
            intent_id=
                intent_id
        )
    )

    return {
        "intent_id":
            intent_id,

        "count":
            len(events),

        "events":
            events,
    }


# ============================================================
# API COMPATIBILITY ROUTES
# ============================================================

@app.post(
    "/api/payment/evaluate"
)
def api_payment_evaluate(
    request: PaymentRequest,
):

    return payment_request(
        request
    )


@app.post(
    "/api/payment/create"
)
def api_payment_create(
    request: PaymentRequest,
    request_obj: Request,
):

    return create_agent_payment(
        request,
        request_obj,
    )