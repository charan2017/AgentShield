import os
from pathlib import Path

import razorpay
from dotenv import load_dotenv


# ============================================================
# LOAD ENVIRONMENT VARIABLES
# ============================================================

# Project root:
# C:\Users\Admin\OneDrive\Desktop\AgentShield\AgentShield

BASE_DIR = Path(__file__).resolve().parents[2]

ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)


# ============================================================
# RAZORPAY CREDENTIALS
# ============================================================

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")


# ============================================================
# GET RAZORPAY CLIENT
# ============================================================

def get_razorpay_client():
    """
    Create and return a Razorpay SDK client.

    Key ID and Key Secret are loaded server-side
    from the .env file.
    """

    if not RAZORPAY_KEY_ID:
        raise RuntimeError(
            f"RAZORPAY_KEY_ID is missing. "
            f"Expected .env at: {ENV_FILE}"
        )

    if not RAZORPAY_KEY_SECRET:
        raise RuntimeError(
            f"RAZORPAY_KEY_SECRET is missing. "
            f"Expected .env at: {ENV_FILE}"
        )

    return razorpay.Client(
        auth=(
            RAZORPAY_KEY_ID,
            RAZORPAY_KEY_SECRET,
        )
    )


# ============================================================
# CREATE RAZORPAY ORDER
# ============================================================

def create_order(
    amount_inr: float,
    receipt: str,
    notes: dict | None = None,
):
    """
    Create a Razorpay order.

    Razorpay expects the amount in paise.
    Example:
        ₹100 = 10000 paise
    """

    client = get_razorpay_client()

    amount_paise = int(
        round(amount_inr * 100)
    )

    data = {
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt,
        "notes": notes or {},
    }

    return client.order.create(
        data=data
    )


# ============================================================
# VERIFY PAYMENT SIGNATURE
# ============================================================

def verify_payment_signature(
    order_id: str,
    payment_id: str,
    signature: str,
):
    """
    Verify the Razorpay Checkout signature.

    This must happen on the backend.
    """

    client = get_razorpay_client()

    verification_data = {
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": signature,
    }

    return client.utility.verify_payment_signature(
        verification_data
    )
def verify_webhook_signature(
    payload: str,
    signature: str,
    webhook_secret: str,
):
    """
    Verify a Razorpay webhook signature.
    """

    client = get_razorpay_client()

    return client.utility.verify_webhook_signature(
        payload,
        signature,
        webhook_secret,
    )