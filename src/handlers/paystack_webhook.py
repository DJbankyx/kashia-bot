# src/handlers/paystack_webhook.py
"""Paystack Webhook Handler — receives payment confirmations and upgrades users."""

import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """
    Paystack sends a POST when payment is successful.
    We verify the signature, extract phone + plan, and upgrade the user.
    """
    try:
        # Verify webhook signature
        from utils.config import get_paystack_secret
        from services.paystack import PaystackService

        secret = get_paystack_secret()
        headers = event.get('headers', {}) or {}
        signature = headers.get('x-paystack-signature', '') or headers.get('X-Paystack-Signature', '')
        body = event.get('body', '') or ''

        if not PaystackService.verify_webhook_signature(body, signature, secret):
            logger.warning("Invalid Paystack webhook signature")
            return response(401, {"error": "Invalid signature"})

        # Parse event
        payload = json.loads(body)
        event_type = payload.get("event", "")

        if event_type != "charge.success":
            # We only care about successful charges
            logger.info(f"Paystack event ignored: {event_type}")
            return response(200, {"status": "ignored"})

        # Extract payment data
        data = payload.get("data", {})
        metadata = data.get("metadata", {})
        phone_number = metadata.get("phone_number", "")
        plan = metadata.get("plan", "")
        amount = data.get("amount", 0)
        reference = data.get("reference", "")

        if not phone_number or not plan:
            logger.error(f"Missing phone/plan in webhook: {metadata}")
            return response(200, {"status": "missing_data"})

        logger.info(f"Payment received: {phone_number} → {plan} (₦{amount/100:,.0f}) ref={reference}")

        # Upgrade the user
        from services.database import Database
        from services.tier_manager import TierManager
        from services.whatsapp_client import WhatsAppClient

        db = Database()
        tier_mgr = TierManager(database=db)
        whatsapp = WhatsAppClient()

        # Perform upgrade
        tier_mgr.upgrade_user(phone_number, plan)

        # Notify user via WhatsApp
        plan_name = "Basic" if plan == "basic" else "Pro"
        whatsapp.send_text(phone_number, (
            f"🎉 *Upgrade Successful!*\n\n"
            f"You're now on the *{plan_name}* plan.\n\n"
            f"✅ Unlimited transactions\n"
            f"✅ Unlimited exports\n"
            f"{'✅ Unlimited invoices' if plan == 'pro' else '✅ 10 invoices/month'}\n"
            f"✅ PDF financial statements\n\n"
            f"Thank you for supporting Kashia! 🙏\n\n"
            f"_Ref: {reference}_"
        ))

        logger.info(f"User upgraded: {phone_number} → {plan_name}")
        return response(200, {"status": "success", "phone": phone_number, "plan": plan})

    except Exception as e:
        logger.error(f"Paystack webhook error: {e}")
        return response(200, {"status": "error"})


def response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps(body)
    }
