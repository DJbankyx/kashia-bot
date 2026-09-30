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
        purpose = str(metadata.get("purpose", "") or "").lower()
        amount = data.get("amount", 0)
        reference = data.get("reference", "")

        # ── PAYMENT COLLECTION branch ──
        # A charge from a Kashia pay-link (invoice/bill a customer paid) carries
        # metadata.purpose == "collection" + owner_id + payreq_id (see
        # PaystackService.initialize_collection). Route it to the collection
        # handler instead of the subscription-upgrade path. This is exactly the
        # spot a non-plan charge used to fall into the 'missing_data' dead end.
        if purpose == "collection":
            return _handle_collection(metadata, data, reference)

        # ── SUBSCRIPTION UPGRADE (existing path) ──
        phone_number = metadata.get("phone_number", "")
        plan = metadata.get("plan", "")

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
        # NB: the reference is FULL of underscores (kashia_basic_monthly_tg_..._...).
        # send_text() renders with Telegram parse_mode=Markdown, which reads each
        # "_" as an italic delimiter — an odd count = unterminated entity = HTTP
        # 400 "can't parse entities", and the WHOLE confirmation is rejected (the
        # user paid + was upgraded but got NO message). Putting the ref on its own
        # line does NOT help: Markdown ignores line boundaries. Escape every "_"
        # (and "*") in the ref so Markdown treats it as literal text. Verified the
        # only underscores in this message live in the ref; the *bold* labels are
        # balanced.
        safe_ref = str(reference).replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*")
        client.send_text(recipient, (
            f"🎉 *Upgrade Successful!*\n\n"
            f"You're now on the *{plan_name}* plan ({period_word}ly).\n\n"
            f"✅ Unlimited transactions\n"
            f"✅ Unlimited exports\n"
            f"{'✅ Unlimited invoices' if plan == 'pro' else '✅ 10 invoices/month'}\n"
            f"✅ PDF financial statements\n\n"
            f"{renew_line}"
            f"Thank you for supporting Kashia! 🙏\n\n"
            f"Ref: {safe_ref}"
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


def _handle_collection(metadata, data, reference):
    """Handle a PAID payment-collection charge (a customer paid a Kashia pay-link
    for an invoice/bill). Records a paid SALE, AUTO-SETTLES the customer's
    receivable if one exists (the sticky auto-reconciliation feature), marks the
    PaymentRequest paid, and notifies the owner. Idempotent on the Paystack
    reference. See docs/PAYMENT_COLLECTION_PLAN.md.

    Returns an HTTP response dict. On a genuine post-claim failure it returns 500
    (after releasing the claim) so Paystack retries safely.
    """
    from services.database import Database
    from services.whatsapp_client import WhatsAppClient
    from services.messaging_client import resolve_client
    from utils.money import money_round

    owner_id = metadata.get("owner_id") or metadata.get("phone_number") or ""
    payreq_id = metadata.get("payreq_id") or ""
    customer_name = (metadata.get("customer_name") or "").strip()
    description = metadata.get("description") or "Payment received"
    # GROSS amount Paystack charged the customer (integer kobo) → naira. This
    # INCLUDES Paystack's own processing fee, which the customer pays on top of
    # what the owner requested (fees=none on our side). It is NOT what the owner
    # sold — recording it as revenue would overstate the books by the fee.
    kobo = data.get("amount", 0) if isinstance(data, dict) else 0
    gross_naira = money_round((int(kobo) if kobo else 0) / 100.0)

    if not owner_id:
        logger.error(f"Collection webhook missing owner_id: {metadata}")
        return response(200, {"status": "missing_data"})

    db = Database()
    whatsapp = WhatsAppClient()

    # ── IDEMPOTENCY (shared guard, keyed on the Paystack reference) ──
    if reference:
        claimed, _prior = db.claim_web_submit(owner_id, f"paystack#{reference}")
        if not claimed:
            logger.info(f"Collection webhook DUPLICATE ignored: ref={reference}")
            return response(200, {"status": "duplicate", "reference": reference})

    try:
        # Belt-and-braces on top of the idempotency claim: if the request is
        # already marked paid, don't double-record.
        preq = db.get_payment_request(owner_id, payreq_id) if payreq_id else None
        if preq and str(preq.get("status")) == "paid":
            logger.info(f"Payment request {payreq_id} already paid — skipping.")
            return response(200, {"status": "already_paid"})

        # ── HONEST AMOUNT (Fix C) ──
        # Record what the owner ASKED FOR (the stored request amount), NOT the
        # fee-inflated gross Paystack charged the customer. The difference is
        # Paystack's processing fee (the customer covers it; we add no markup).
        # Recording the gross would overstate revenue by that fee. Fall back to
        # the gross only if we can't read the stored request (shouldn't happen —
        # the link isn't handed out unless create_payment_request persisted).
        requested_naira = None
        if preq is not None:
            try:
                requested_naira = money_round(preq.get("amount", 0) or 0)
            except Exception:
                requested_naira = None
        amount_naira = requested_naira if (requested_naira and requested_naira > 0) else gross_naira
        # Fee the customer paid on top (>= 0). Purely informational in the books;
        # the owner neither earns nor pays it — Paystack takes it from the payer.
        fee_naira = money_round(gross_naira - amount_naira)
        if fee_naira < 0:
            fee_naira = 0

        # AUTO-SETTLE vs fresh sale (owner decision: auto-reconcile). If the
        # customer has an open receivable, the payment SETTLES it (a REPAYMENT,
        # NOT new revenue — the original credit sale already recognised the
        # income). Only the portion BEYOND any debt is a genuine new sale.
        # Settlement uses the REQUESTED amount (never the fee-inflated gross).
        #
        # ACCOUNTING-CRITICAL: a settled portion must be recorded so
        # accounting._is_debt_settlement recognises it (description "Debt
        # repayment from …" / category "Debt Repayment"), EXACTLY like the chat
        # debt._apply_directed_payment does — otherwise it's double-counted as
        # revenue on top of the original credit sale (the Muyideen bug).
        settled = 0
        if customer_name:
            try:
                contact = db.get_contact_by_name(owner_id, customer_name)
                owed = money_round((contact or {}).get("debt_owed_to_me", 0) or 0)
                if owed > 0:
                    pay = amount_naira if amount_naira <= owed else owed
                    db.settle_debt(owner_id, customer_name, pay, "owed_to_me")
                    settled = pay
            except Exception as e:
                logger.warning(f"collection settle_debt failed: {e}")

        # The part of the payment that is NOT settling a debt = a real new sale.
        sale_portion = money_round(amount_naira - settled)
        if sale_portion < 0:
            sale_portion = 0

        common_extra = {"source": "payment_collection", "paystack_ref": reference,
                        "payreq_id": payreq_id, "gross_charged": gross_naira,
                        "paystack_fee": fee_naira}
        paid_tx_id = ""

        # 1) Settled portion → a REPAYMENT row (kept OUT of P&L). Mirror the chat:
        #    type='sale', description "Debt repayment from {name}" so
        #    _is_debt_settlement catches it (it also cash-tracks the collection).
        if settled > 0:
            rep = db.save_transaction(
                owner_id, settled, "sale",
                f"Debt repayment from {customer_name}", "Debt Repayment",
                vendor=customer_name, payment_method="cash",
                extra_details=dict(common_extra, settled_receivable=settled,
                                   kind="debt_repayment"),
            )
            paid_tx_id = (rep or {}).get("transaction_id", "")

        # 2) Remainder (or the whole thing when there was no debt) → a real SALE.
        if sale_portion > 0 or settled == 0:
            sale = db.save_transaction(
                owner_id, (sale_portion if settled > 0 else amount_naira), "sale",
                description, "Sales & Income",
                vendor=customer_name, payment_method="cash",
                extra_details=dict(common_extra, settled_receivable=settled),
            )
            # Prefer the sale's id as the primary paid_tx_id when there is one.
            paid_tx_id = (sale or {}).get("transaction_id", "") or paid_tx_id

        if payreq_id:
            db.mark_payment_request_paid(owner_id, payreq_id, paid_tx_id)
    except Exception:
        # Post-claim failure on an authentic webhook → release the claim so the
        # 500-triggered Paystack retry can re-process, then re-raise to 500.
        if reference:
            db.release_web_submit(owner_id, f"paystack#{reference}")
        logger.error("Collection processing failed; releasing claim for retry")
        return response(500, {"status": "error", "retry": True})

    # Notify the owner on their own platform. Escape the underscore-heavy ref for
    # Telegram Markdown (an odd count of "_" = 400 "can't parse entities").
    try:
        client, recipient = resolve_client(owner_id, whatsapp_fallback=whatsapp)
        if client is None:
            client, recipient = whatsapp, owner_id
        who = f" from *{customer_name}*" if customer_name else ""
        settle_line = (f"\n\u2705 Settled \u20a6{settled:,.0f} of what they owed."
                       if settled else "")
        # Only mention the fee when there was one, so the owner understands why
        # their Paystack dashboard shows a bigger number than what we booked.
        fee_line = (f"\n\u2139\uFE0F Customer also paid \u20a6{fee_naira:,.0f} "
                    f"Paystack fee (not counted as your income)."
                    if fee_naira else "")
        # Book line reflects what actually happened so a REPAYMENT isn't called a
        # sale (that was the confusing part): all-debt → repayment; mixed → both;
        # no-debt → sale.
        if settled and sale_portion <= 0:
            book_line = ("It's recorded as a debt repayment (not new income \u2014 "
                         "the original sale already counted).")
        elif settled and sale_portion > 0:
            book_line = (f"\u20a6{settled:,.0f} clears old debt; "
                         f"\u20a6{sale_portion:,.0f} is a new paid sale.")
        else:
            book_line = "It's recorded as a paid sale in your books."
        safe_ref = str(reference).replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*")
        client.send_text(recipient, (
            f"\U0001F4B0 *Payment received!*\n\n"
            f"\u20a6{amount_naira:,.0f}{who} for {description}.{settle_line}{fee_line}\n\n"
            f"{book_line}\n\n"
            f"\U0001F9FE Need a receipt? Open the app \u2192 Customers \u2192 "
            f"\U0001F4B3 Payment links \u2192 tap \U0001F9FE Receipt on this payment.\n\n"
            f"Ref: {safe_ref}"
        ))
    except Exception as e:
        # Notification is best-effort; the money is already recorded. Do NOT 500
        # (that would make Paystack retry a fully-processed charge).
        logger.warning(f"collection notify failed: {e}")

    logger.info(f"Collection recorded: {owner_id} ₦{amount_naira} "
                f"(gross ₦{gross_naira}, fee ₦{fee_naira}) "
                f"settled={settled} ref={reference}")
    return response(200, {"status": "success", "owner": owner_id,
                          "amount": amount_naira, "settled": settled})


def response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps(body)
    }
