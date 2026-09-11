# src/services/paystack.py
"""Paystack payment integration — initialize transactions and verify payments."""

import hashlib
import hmac
import json
import logging
import requests

from utils.config import get_paystack_secret

logger = logging.getLogger(__name__)

PAYSTACK_BASE_URL = "https://api.paystack.co"


def _paystack_safe(user_id: str) -> str:
    """Make a user id safe for a Paystack reference / email local-part.

    Replaces any character that isn't alphanumeric with an underscore. This is
    only for the derived reference/email fields; the original id is kept intact
    in the transaction metadata (used for routing + the tier upgrade).
    Example: "tg:12345678" -> "tg_12345678".
    """
    import re
    return re.sub(r"[^A-Za-z0-9]", "_", str(user_id or ""))

# Plan amounts in kobo (Paystack uses kobo = naira × 100), per billing PERIOD.
# Yearly = pay for 10 months (owner decision: 2 months free); quarterly carries a
# small discount. Monthly is the default and back-compat baseline.
#   Basic: 3,000/mo · 8,500/qtr · 30,000/yr    Pro: 6,000/mo · 17,000/qtr · 60,000/yr
PLANS = {
    "basic": {
        "name": "Kashia Basic",
        "periods": {
            "monthly":   {"amount": 300000,  "price_display": "₦3,000/month"},
            "quarterly": {"amount": 850000,  "price_display": "₦8,500/quarter"},
            "yearly":    {"amount": 3000000, "price_display": "₦30,000/year"},
        },
    },
    "pro": {
        "name": "Kashia Pro",
        "periods": {
            "monthly":   {"amount": 600000,   "price_display": "₦6,000/month"},
            "quarterly": {"amount": 1700000,  "price_display": "₦17,000/quarter"},
            "yearly":    {"amount": 6000000,  "price_display": "₦60,000/year"},
        },
    },
}

# Order the picker offers periods in.
PERIODS = ("monthly", "quarterly", "yearly")


def plan_period_amount(plan: str, period: str):
    """(amount_kobo, price_display) for a plan+period, defaulting to monthly.
    Returns (None, None) for an unknown plan."""
    p = PLANS.get(plan)
    if not p:
        return None, None
    period = str(period or "monthly").lower()
    pd = p["periods"].get(period) or p["periods"]["monthly"]
    return pd["amount"], pd["price_display"]


class PaystackService:
    """Handles Paystack payment initialization and verification."""

    def __init__(self):
        self.secret_key = None  # Lazy-load

    def _get_headers(self) -> dict:
        if not self.secret_key:
            self.secret_key = get_paystack_secret()
        return {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }

    def initialize_transaction(self, phone_number: str, plan: str, email: str = None,
                               period: str = "monthly") -> dict:
        """
        Create a Paystack payment link for a user.

        Args:
            phone_number: user's id (namespaced for Telegram)
            plan: "basic" or "pro"
            email: user's email (optional — Paystack requires one)
            period: "monthly" | "quarterly" | "yearly" (default monthly)

        Returns:
            {"success": True, "payment_url": "https://...", "reference": "..."}
            or {"success": False, "error": "..."}
        """
        if plan not in PLANS:
            return {"success": False, "error": f"Invalid plan: {plan}"}

        period = str(period or "monthly").lower()
        amount, price_display = plan_period_amount(plan, period)
        if amount is None:
            return {"success": False, "error": f"Invalid plan: {plan}"}

        # The user id may be namespaced (e.g. "tg:12345678" for Telegram users).
        # Paystack references and email local-parts must not contain characters
        # like ":", so we derive a safe token for THOSE fields only. The full
        # id is preserved verbatim in metadata below, which is what the webhook
        # uses to route the confirmation and key the upgrade.
        safe_id = _paystack_safe(phone_number)

        # Use the safe id as an email fallback (Paystack requires an email)
        if not email:
            email = f"{safe_id}@kashia.app"

        # Reference carries plan + PERIOD so the webhook knows how long to extend
        # (metadata carries them too — the webhook prefers metadata, ref is a
        # fallback / audit trail). alphanumerics + separators only.
        import time
        reference = f"kashia_{plan}_{period}_{safe_id}_{int(time.time())}"

        payload = {
            "email": email,
            "amount": amount,
            "reference": reference,
            "callback_url": "https://kashia.app/payment/success",
            "metadata": {
                "phone_number": phone_number,
                "plan": plan,
                "period": period,
                "custom_fields": [
                    {"display_name": "Phone", "variable_name": "phone", "value": phone_number},
                    {"display_name": "Plan", "variable_name": "plan", "value": plan},
                    {"display_name": "Period", "variable_name": "period", "value": period},
                ]
            }
        }

        try:
            resp = requests.post(
                f"{PAYSTACK_BASE_URL}/transaction/initialize",
                headers=self._get_headers(),
                json=payload,
                timeout=10
            )

            if resp.status_code == 200:
                data = resp.json().get("data", {})
                return {
                    "success": True,
                    "payment_url": data.get("authorization_url", ""),
                    "reference": data.get("reference", reference),
                }
            else:
                logger.error(f"Paystack init error: {resp.status_code} {resp.text}")
                return {"success": False, "error": f"Payment service error ({resp.status_code})"}

        except Exception as e:
            logger.error(f"Paystack request error: {e}")
            return {"success": False, "error": str(e)}

    def verify_transaction(self, reference: str) -> dict:
        """
        Verify a payment was successful.
        
        Returns:
            {"success": True, "phone_number": "...", "plan": "...", "amount": ...}
            or {"success": False, "error": "..."}
        """
        try:
            resp = requests.get(
                f"{PAYSTACK_BASE_URL}/transaction/verify/{reference}",
                headers=self._get_headers(),
                timeout=10
            )

            if resp.status_code == 200:
                data = resp.json().get("data", {})
                if data.get("status") == "success":
                    metadata = data.get("metadata", {})
                    return {
                        "success": True,
                        "phone_number": metadata.get("phone_number", ""),
                        "plan": metadata.get("plan", ""),
                        "amount": data.get("amount", 0),
                        "reference": reference,
                    }
                else:
                    return {"success": False, "error": f"Payment status: {data.get('status')}"}
            else:
                return {"success": False, "error": f"Verification failed ({resp.status_code})"}

        except Exception as e:
            logger.error(f"Paystack verify error: {e}")
            return {"success": False, "error": str(e)}

    @staticmethod
    def verify_webhook_signature(payload_body: str, signature: str, secret_key: str) -> bool:
        """Verify that a webhook request is genuinely from Paystack."""
        if not signature or not secret_key:
            return False
        computed = hmac.new(
            secret_key.encode('utf-8'),
            payload_body.encode('utf-8'),
            hashlib.sha512
        ).hexdigest()
        return hmac.compare_digest(signature, computed)
