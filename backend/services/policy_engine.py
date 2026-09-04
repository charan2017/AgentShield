from typing import Dict, Any

from backend.services.security_config import (
    get_policy,
)


def evaluate_policy(
    policy: Dict[str, Any] | None,
    amount: float,
    category: str,
) -> Dict[str, Any]:
    # Runtime configuration from Settings
    runtime_policy = get_policy()

    max_per_transaction = runtime_policy.get(
        "max_per_transaction",
        5000,
    )

    daily_limit = runtime_policy.get(
        "daily_limit",
        15000,
    )

    allowed_categories = runtime_policy.get(
        "allowed_categories",
        [],
    )

    approval_threshold = runtime_policy.get(
        "approval_threshold",
        3000,
    )

    if amount > max_per_transaction:
        return {
            "decision": "BLOCKED",
            "reason": (
                "Amount exceeds the "
                "current per-transaction limit."
            ),
            "requires_user_approval": False,
        }

    if category.lower() not in [
        item.lower()
        for item in allowed_categories
    ]:
        return {
            "decision": "BLOCKED",
            "reason": (
                "Category is not allowed by policy."
            ),
            "requires_user_approval": False,
        }

    if amount > approval_threshold:
        return {
            "decision": "APPROVED",
            "reason": (
                "Payment requires human approval "
                "under the current policy."
            ),
            "requires_user_approval": True,
        }

    return {
        "decision": "APPROVED",
        "reason": (
            "Payment is within the current "
            "delegated authorization policy."
        ),
        "requires_user_approval": False,
    }