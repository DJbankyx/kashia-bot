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

# Plan amounts in kobo (Paystack uses kobo = naira × 100)
PLANS = {
    "basic": {"amount": 300000, "name": "Kashia Basic", "price_display": "₦3,000/month"},
    "pro":   {"amount": 600000, "name": "Kashia Pro",   "price_display": "₦6,000/month"},
}


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

    def initialize_transaction(self, phone_number: str, plan: str, email: str = None) -> dict:
        """
        Create a Paystack payment link for a user.
        
        Args:
            phone_number: user's phone (used as reference)
            plan: "basic" or "pro"
            email: user's email (optional — Paystack requires one)
            
        Returns:
            {"success": True, "payment_url": "https://...", "reference": "..."}
            or {"success": False, "error": "..."}
        """
        if plan not in PLANS:
            return {"success": False, "error": f"Invalid plan: {plan}"}

        plan_data = PLANS[plan]

        # Use phone as email fallback (Paystack requires email)
        if not email:
            email = f"{phone_number}@kashia.app"

        # Generate unique reference
        import time
        reference = f"kashia_{plan}_{phone_number}_{int(time.time())}"

        payload = {
            "email": email,
            "amount": plan_data["amount"],
            "reference": reference,
            "callback_url": "https://kashia.app/payment/success",
            "metadata": {
                "phone_number": phone_number,
                "plan": plan,
                "custom_fields": [
                    {"display_name": "Phone", "variable_name": "phone", "value": phone_number},
                    {"display_name": "Plan", "variable_name": "plan", "value": plan},
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
