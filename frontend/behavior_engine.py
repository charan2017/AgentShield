import hashlib
import time
from dataclasses import dataclass
from threading import Lock


@dataclass
class BehaviorResult:
    score: int
    level: str
    action: str
    attempt_count: int
    repeated_attempts: int
    reason: str


class AgentBehaviorEngine:
    """
    Tracks payment behavior for each
    agent + session combination.
    """

    def __init__(
        self,
        window_seconds: int = 60,
        warning_threshold: int = 3,
        block_threshold: int = 4,
    ):
        self.window_seconds = window_seconds
        self.warning_threshold = warning_threshold
        self.block_threshold = block_threshold

        self._events = {}
        self._lock = Lock()

    def _fingerprint(
        self,
        recipient: str,
        amount: float,
        category: str,
    ) -> str:

        raw = (
            f"{recipient.strip().lower()}|"
            f"{amount:.2f}|"
            f"{category.strip().lower()}"
        )

        return hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()

    def _cleanup(
        self,
        events: list,
        now: float,
    ) -> list:

        cutoff = now - self.window_seconds

        return [
            event
            for event in events
            if event["timestamp"] >= cutoff
        ]

    def evaluate(
        self,
        agent_id: str,
        session_id: str,
        recipient: str,
        amount: float,
        category: str,
    ) -> BehaviorResult:

        now = time.time()

        key = f"{agent_id}:{session_id}"

        fingerprint = self._fingerprint(
            recipient=recipient,
            amount=amount,
            category=category,
        )

        with self._lock:

            events = self._events.get(
                key,
                [],
            )

            events = self._cleanup(
                events,
                now,
            )

            events.append(
                {
                    "timestamp": now,
                    "fingerprint": fingerprint,
                    "recipient": recipient,
                    "amount": amount,
                    "category": category,
                    "type": "PAYMENT_REQUEST",
                }
            )

            self._events[key] = events

        attempt_count = len(events)

        repeated_attempts = sum(
            1
            for event in events
            if event["fingerprint"] == fingerprint
        )

        score = 0

        # Repeated identical payment
        if repeated_attempts >= 2:
            score += 25

        if repeated_attempts >= 3:
            score += 25

        if repeated_attempts >= 4:
            score += 30

        # High payment velocity
        if attempt_count >= 3:
            score += 15

        if attempt_count >= 5:
            score += 15

        score = min(score, 100)

        # ----------------------------------------------------
        # Decision
        # ----------------------------------------------------

        if repeated_attempts >= self.block_threshold:

            level = "HIGH"

            action = "BLOCK"

            reason = (
                "Agent payment loop detected: "
                f"the same payment was attempted "
                f"{repeated_attempts} times within "
                f"{self.window_seconds} seconds."
            )

        elif repeated_attempts >= self.warning_threshold:

            level = "MEDIUM"

            action = "REVIEW"

            reason = (
                "Repeated payment behavior detected: "
                f"{repeated_attempts} attempts for "
                "the same payment."
            )

        elif attempt_count >= 5:

            level = "MEDIUM"

            action = "REVIEW"

            reason = (
                "High payment-request velocity "
                "detected for this agent session."
            )

        elif score >= 40:

            level = "MEDIUM"

            action = "REVIEW"

            reason = (
                "Agent behavior is becoming unusual."
            )

        else:

            level = "LOW"

            action = "ALLOW"

            reason = (
                "Agent behavior is within the "
                "expected session pattern."
            )

        return BehaviorResult(
            score=score,
            level=level,
            action=action,
            attempt_count=attempt_count,
            repeated_attempts=repeated_attempts,
            reason=reason,
        )

    def clear_session(
        self,
        agent_id: str,
        session_id: str,
    ):

        key = f"{agent_id}:{session_id}"

        with self._lock:
            self._events.pop(key, None)


behavior_engine = AgentBehaviorEngine()