# src/features/payment_collection.py
"""Chat 'Request payment' flow (Phase 3) — mint a Paystack pay-link the owner
forwards to a customer. When the customer pays, the Paystack webhook
(_handle_collection) auto-records the sale, settles the customer's debt, and
notifies the owner. Shared engine; the flow itself is platform-neutral.

Steps: amount → description → customer (optional) → create link + send it.
Reuses PaystackService.initialize_collection + db.create_payment_request (same
engine the Mini App uses), so there is no forked money logic.
"""

import logging
import uuid

from core import states
from utils.parser import parse_amount
from utils.whatsapp_ui import text_response
from utils.money import money_round

logger = logging.getLogger(__name__)


class PaymentCollectionHandler:
    """Guided chat flow to create a payment-collection pay-link."""

    def __init__(self, session_mgr, database, paystack_service):
        self.session = session_mgr
        self.db = database
        self.paystack = paystack_service

    def start(self, phone_number: str) -> list:
        """Begin the request-payment flow."""
        self.session.save(phone_number, states.REQUEST_PAYMENT, {"pc_step": "amount"})
        return [text_response(
            "💳 *Request a payment*\n\n"
            "I'll create a link your customer can pay online. When they pay, I "
            "record it and settle their balance automatically.\n\n"
            "How much are you collecting? (e.g. _2500_)\n\n"
            "Or type *cancel*."
        )]

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        context = session.get("context", {})
        step = context.get("pc_step", "amount")

        if text.strip().lower() in ("cancel", "exit", "back"):
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled.")]

        if step == "amount":
            amount = parse_amount(text)
            if not amount or amount <= 0:
                return [text_response(
                    "💰 Please enter a valid amount, e.g. _2500_.\n\nOr type *cancel*.")]
            context["pc_amount"] = money_round(amount)
            context["pc_step"] = "description"
            self.session.save(phone_number, states.REQUEST_PAYMENT, context)
            return [text_response(
                "📝 What's it for? (e.g. _Invoice for 5 bags of rice_)\n\n"
                "Or type *skip*.")]

        if step == "description":
            desc = text.strip()
            if desc.lower() in ("skip", "none", "-"):
                desc = "Payment"
            context["pc_desc"] = desc
            context["pc_step"] = "customer"
            self.session.save(phone_number, states.REQUEST_PAYMENT, context)
            return [text_response(
                "👤 Who's paying? Type the customer's name so I can settle their "
                "balance when they pay.\n\nOr type *skip*.")]

        if step == "customer":
            customer = text.strip()
            if customer.lower() in ("skip", "none", "-"):
                customer = ""
            return self._create_link(phone_number, context, customer)

        # Unknown step → restart cleanly.
        self.session.reset(phone_number)
        return self.start(phone_number)

    def _create_link(self, phone_number: str, context: dict, customer: str) -> list:
        """Mint the pay-link + persist the request, then hand the owner the link."""
        amount = context.get("pc_amount") or 0
        desc = context.get("pc_desc") or "Payment"
        self.session.reset(phone_number)
        try:
            payreq_id = "payreq#" + uuid.uuid4().hex[:12]
            res = self.paystack.initialize_collection(
                phone_number, float(amount), desc, payreq_id,
                customer_name=customer or None)
            if not res.get("success"):
                return [text_response(
                    "❌ Couldn't create the payment link right now. Please try again.")]
            ref = res["reference"]
            url = res["payment_url"]
            stored = self.db.create_payment_request(
                phone_number, float(amount), desc, payreq_id, ref,
                customer_name=customer or None, payment_url=url)
            if not stored:
                return [text_response(
                    "⚠️ The link was created but I couldn't save the request, so it "
                    "won't reconcile automatically. Please try again.")]

            who = f" to *{customer}*" if customer else ""
            # The pay-link is underscore/query-heavy; send it on its own line and
            # WITHOUT markdown around it. text_response goes out as Markdown, so a
            # bare URL is fine (no wrapping markup to break), and the client sets
            # disable_web_page_preview. Keep the amount bold, the URL plain.
            return [text_response(
                f"✅ *Payment link ready*{who}\n\n"
                f"💰 Amount: *₦{float(amount):,.0f}*\n"
                f"📝 For: {desc}\n\n"
                f"Send this link to your customer:\n"
                f"{url}\n\n"
                f"When they pay, I'll record it as a paid sale"
                f"{' and settle their balance' if customer else ''}, and message you."
            )]
        except Exception as e:
            logger.error(f"request-payment create link failed: {e}")
            return [text_response("❌ Something went wrong creating the link. Please try again.")]
