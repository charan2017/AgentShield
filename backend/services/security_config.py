from __future__ import annotations

from pathlib import Path
import json
from threading import Lock
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_FILE = BASE_DIR / "security_config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "max_per_transaction": 5000,
    "daily_limit": 15000,
    "approval_threshold": 3000,
    "behavior_monitoring": True,
    "action_ledger": True,
    "idempotency": True,
}

_lock = Lock()


def _ensure_config_file() -> None:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(
                DEFAULT_CONFIG,
                indent=2,
            ),
            encoding="utf-8",
        )


def get_config() -> dict[str, Any]:
    with _lock:
        _ensure_config_file()

        try:
            data = json.loads(
                CONFIG_FILE.read_text(
                    encoding="utf-8"
                )
            )
        except (
            OSError,
            json.JSONDecodeError,
        ):
            data = {}

        config = {
            **DEFAULT_CONFIG,
            **data,
        }

        return config


def get_policy() -> dict[str, Any]:
    config = get_config()

    return {
        "max_per_transaction":
            int(config["max_per_transaction"]),
        "daily_limit":
            int(config["daily_limit"]),
        "approval_threshold":
            int(config["approval_threshold"]),
        "allowed_categories": [
            "hotel",
            "flight",
            "food",
            "personal_transfer",
        ],
    }


def update_config(
    updates: dict[str, Any],
) -> dict[str, Any]:
    with _lock:
        _ensure_config_file()

        try:
            existing = json.loads(
                CONFIG_FILE.read_text(
                    encoding="utf-8"
                )
            )
        except (
            OSError,
            json.JSONDecodeError,
        ):
            existing = {}

        config = {
            **DEFAULT_CONFIG,
            **existing,
        }

        numeric_fields = {
            "max_per_transaction",
            "daily_limit",
            "approval_threshold",
        }

        for field in numeric_fields:
            if field in updates:
                value = int(updates[field])

                if value <= 0:
                    raise ValueError(
                        f"{field} must be greater than zero."
                    )

                config[field] = value

        boolean_fields = {
            "behavior_monitoring",
            "action_ledger",
            "idempotency",
        }

        for field in boolean_fields:
            if field in updates:
                config[field] = bool(
                    updates[field]
                )

        if (
            config["approval_threshold"]
            > config["max_per_transaction"]
        ):
            raise ValueError(
                "Human approval threshold cannot exceed the transaction limit."
            )

        if (
            config["max_per_transaction"]
            > config["daily_limit"]
        ):
            raise ValueError(
                "Transaction limit cannot exceed the daily spending limit."
            )

        CONFIG_FILE.write_text(
            json.dumps(
                config,
                indent=2,
            ),
            encoding="utf-8",
        )

        return config