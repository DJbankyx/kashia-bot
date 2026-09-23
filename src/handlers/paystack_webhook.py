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
    # Tracks whether we've passed signature + parse. If a failure happens AFTER
    # this (a genuine processing error on a valid, authentic webhook), we return
    # 500 so Paystack RETRIES — otherwise the user paid but was never upgraded,
    # silently. Before this point (bad signature / unparseable body) we never
    # want a retry. The idempotency guard above makes retries safe.
    verified_and_parsed = False
    try:
        # Verify webhook signature
        from utils.config import get_paystack_secret
        from services.paystack import PaystackService

        secret = get_paystack_secret()
        headers = event.get('headers', {}) or {}
        signature = headers.get('x-paystack-signature', '') or headers.get('X-Paystack-Signature', '')
        body = event.get('body', '') or ''
        # API Gateway may deliver the body base64-encoded. Paystack's HMAC is
        # computed over the RAW JSON it sent, so decode first or the signature
        # check would fail on every genuine webhook (silent "no upgrade").
        if event.get('isBase64Encoded') and body:
            try:
                import base64
                body = base64.b64decode(body).decode('utf-8')
            except Exception as e:
                logger.error(f"Paystack webhook: body b64 decode failed: {e}")

        if not PaystackService.verify_webhook_signature(body, signature, secret):
            logger.warning("Invalid Paystack webhook signature")
            return response(401, {"error": "Invalid signature"})

        # Parse event
        payload = json.loads(body)
        # Past signature + parse: any failure from here is a genuine processing
        # error on an AUTHENTIC webhook → we want Paystack to retry (return 500).
        verified_and_parsed = True
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

        # Billing PERIOD (S3): prefer metadata; else parse the reference
        # (kashia_{plan}_{period}_{id}_{ts}); else default monthly (back-compat
        # with old monthly-only references kashia_{plan}_{id}_{ts}).
        period = str(metadata.get("period", "") or "").lower()
        if period not in ("monthly", "quarterly", "yearly"):
            period = "monthly"
            try:
                parts = str(reference).split("_")
                if len(parts) >= 3 and parts[2] in ("monthly", "quarterly", "yearly"):
                    period = parts[2]
            except Exception:
                period = "monthly"

        logger.info(f"Payment received: {phone_number} → {plan}/{period} "
                    f"(₦{amount/100:,.0f}) ref={reference}")

        # Upgrade the user
        from services.database import Database
        from services.tier_manager import TierManager
        from services.whatsapp_client import WhatsAppClient
        from services.messaging_client import resolve_client

        db = Database()
        tier_mgr = TierManager(database=db)
        whatsapp = WhatsAppClient()

        # ── IDEMPOTENCY GUARD (money-critical) ──
        # Paystack delivers webhooks at-least-once and RETRIES on timeout/non-2xx.
        # upgrade_user() EXTENDS subscription_ends additively, so processing the
        # same charge twice would give the user double the days for one payment.
        # Atomically CLAIM the Paystack reference (its unique per-charge id) using
        # the same guard the Mini App uses; if already claimed, this webhook is a
        # retry/duplicate — acknowledge 200 and do NOT upgrade again.
        if reference:
            claimed, _prior = db.claim_web_submit(phone_number, f"paystack#{reference}")
            if not claimed:
                logger.info(f"Paystack webhook DUPLICATE ignored: ref={reference} "
                            f"phone={phone_number}")
                return response(200, {"status": "duplicate", "reference": reference})

        # Perform upgrade (period sets how far subscription_ends is extended).
        # If it throws, RELEASE the idempotency claim so the 500-triggered retry
        # can re-process (otherwise the retry would be rejected as a duplicate
        # and the paid user would never get upgraded).
        try:
            tier_mgr.upgrade_user(phone_number, plan, period=period)
        except Exception:
            if reference:
                db.release_web_submit(phone_number, f"paystack#{reference}")
            raise

        # Notify the user on their own platform. `phone_number` here is the
        # namespaced user id carried in the payment metadata (bare phone for
        # WhatsApp, "tg:<chat_id>" for Telegram), so this routes correctly.
        client, recipient = resolve_client(phone_number, whatsapp_fallback=whatsapp)
        if client is None:
            client, recipient = whatsapp, phone_number

        plan_name = "Basic" if plan == "basic" else "Pro"
        period_word = {"monthly": "month", "quarterly": "quarter",
                       "yearly": "year"}.get(period, "month")
        # Show the renewal date so the user knows their cycle.
        try:
            renew_on = (db.get_user(phone_number) or {}).get("subscription_ends", "")
            renew_line = (f"📅 Renews on *{str(renew_on)[:10]}* "
                          f"(billed per {period_word}).\n\n") if renew_on else ""
        except Exception:
            renew_line = ""
        client.send_text(recipient, (
            f"🎉 *Upgrade Successful!*\n\n"
            f"You're now on the *{plan_name}* plan ({period_word}ly).\n\n"
            f"✅ Unlimited transactions\n"
            f"✅ Unlimited exports\n"
            f"{'✅ Unlimited invoices' if plan == 'pro' else '✅ 10 invoices/month'}\n"
            f"✅ PDF financial statements\n\n"
            f"{renew_line}"
            f"Thank you for supporting Kashia! 🙏\n\n"
            f"_Ref: {reference}_"
        ))

        logger.info(f"User upgraded: {phone_number} → {plan_name}/{period}")
        return response(200, {"status": "success", "phone": phone_number, "plan": plan})

    except Exception as e:
        logger.error(f"Paystack webhook error: {e}")
        # If we already verified + parsed, this was a real processing failure on
        # an authentic webhook — return 500 so Paystack retries (the idempotency
        # guard makes that safe). If we failed before that (bad signature /
        # unparseable body), a retry would never help — acknowledge 200.
        if verified_and_parsed:
            return response(500, {"status": "error", "retry": True})
        return response(200, {"status": "error"})


def response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps(body)
    }
