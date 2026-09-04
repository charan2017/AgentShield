import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


INDIA_TZ = ZoneInfo("Asia/Kolkata")


def _parse_amount(command: str):
    patterns = [
        r"(?:pay|send|transfer)\s+(?:₹|rs\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)",
        r"(?:pay|send|transfer).*?(?:₹|rs\.?|inr)\s*([0-9]+(?:\.[0-9]+)?)",
        r"(?:₹|rs\.?|inr)\s*([0-9]+(?:\.[0-9]+)?)",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            command,
            re.IGNORECASE,
        )

        if match:
            return float(match.group(1))

    return None


def _parse_recipient(command: str):
    # Remove reminder prefix so that
    # "remind me to pay 1200 to electricity"
    # does not treat "pay 1200 to electricity"
    # as the recipient.
    cleaned = re.sub(
        r"^\s*remind\s+me\s+to\s+",
        "",
        command,
        flags=re.IGNORECASE,
    )

    # Find every "to ..." occurrence and use the last one.
    matches = list(
        re.finditer(
            r"\bto\s+(.+?)(?=\s+(?:tomorrow|today|monthly|every\s+month|on\s+\d{1,2}(?:st|nd|rd|th)?)\s*$|$)",
            cleaned,
            re.IGNORECASE,
        )
    )

    if matches:
        recipient = matches[-1].group(1).strip()

        recipient = re.sub(
            r"\s+(?:tomorrow|today|monthly|every\s+month)\s*$",
            "",
            recipient,
            flags=re.IGNORECASE,
        )

        recipient = re.sub(
            r"\s+on\s+\d{1,2}(?:st|nd|rd|th)?\s*$",
            "",
            recipient,
            flags=re.IGNORECASE,
        )

        return recipient.strip(" .,") or None

    return None


def _parse_day_of_month(command: str):
    match = re.search(
        r"\bon\s+(\d{1,2})(?:st|nd|rd|th)?\b",
        command,
        re.IGNORECASE,
    )

    if not match:
        return None

    day = int(match.group(1))

    if 1 <= day <= 31:
        return day

    return None


def _parse_schedule(command: str):
    normalized = command.lower().strip()

    day_of_month = _parse_day_of_month(normalized)

    if (
        "monthly" in normalized
        or "every month" in normalized
        or day_of_month is not None
    ):
        return {
            "schedule_type": "MONTHLY",
            "day_of_month": day_of_month,
            "scheduled_at": None,
        }

    if "tomorrow" in normalized:
        now = datetime.now(INDIA_TZ)

        tomorrow = now + timedelta(days=1)

        scheduled_at = tomorrow.replace(
            hour=9,
            minute=0,
            second=0,
            microsecond=0,
        )

        return {
            "schedule_type": "ONE_TIME",
            "day_of_month": None,
            "scheduled_at": scheduled_at.isoformat(),
        }

    if "today" in normalized:
        now = datetime.now(INDIA_TZ)

        scheduled_at = now + timedelta(minutes=1)

        return {
            "schedule_type": "ONE_TIME",
            "day_of_month": None,
            "scheduled_at": scheduled_at.isoformat(),
        }

    return {
        "schedule_type": "IMMEDIATE",
        "day_of_month": None,
        "scheduled_at": None,
    }


def parse_command(command: str):
    if not isinstance(command, str):
        raise ValueError("Command must be a string.")

    command = command.strip()

    if not command:
        raise ValueError("Command cannot be empty.")

    normalized = command.lower()

    amount = _parse_amount(command)
    recipient = _parse_recipient(command)

    schedule = _parse_schedule(command)

    is_reminder = (
        "remind me" in normalized
        or normalized.startswith("remind")
    )

    if is_reminder:
        action = "BILL_REMINDER"
    elif any(
        word in normalized
        for word in [
            "pay",
            "send",
            "transfer",
        ]
    ):
        action = "PAYMENT"
    else:
        action = "UNKNOWN"

    if action == "UNKNOWN":
        raise ValueError(
            "I could not understand the payment command. "
            "Try: 'pay 3000 to Rahul'."
        )

    if amount is None:
        raise ValueError(
            "Payment amount is required."
        )

    if recipient is None:
        raise ValueError(
            "Payment recipient is required."
        )

    return {
        "action": action,
        "amount": amount,
        "currency": "INR",
        "recipient": recipient,
        "schedule_type": schedule["schedule_type"],
        "scheduled_at": schedule["scheduled_at"],
        "day_of_month": schedule["day_of_month"],
        "timezone": "Asia/Kolkata",
        "raw_command": command,
    }