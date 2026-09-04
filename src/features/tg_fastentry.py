# src/features/tg_fastentry.py
"""
Telegram fast-entry — an app-like, tap-first sale/purchase recording flow.

Telegram ONLY. WhatsApp continues to use the existing guided/catalog flows
unchanged; this is gated behind the `tg:` user-id namespace at the router seam.

Design (see docs/TELEGRAM_FLEX_DESIGN.md):
- Presentation only. All business logic (validation, catalog linkage, saving,
  stock, CRM, landing cost) is REUSED from the engine — we assemble a `tx_data`
  dict and converge on `TransactionHandler._build_confirmation()`, after which
  the normal confirm → payment → save chain runs untouched.
- One message, edited in place (Telegram editMessageText) as the user taps,
  instead of a stream of new messages.
- Flow state + the editable message_id live in the session context under
  `__tgfx` (separate from engine state). While collecting, the session state is
  TG_FASTENTRY so text (custom amount) routes back here.

Callback actions (from utils.tg_ui, prefix "__tgfx__"):
  prod:<id>  ppage:<n>  other       (product step)
  amt:<int>  custom                 (amount step)
  pay:<m>                           (payment step)
  back  cancel                      (navigation)
"""

import logging

from core import states
from utils.parser import parse_amount
from utils import tg_ui
from utils.whatsapp_ui import format_amount

logger = logging.getLogger(__name__)

# Session context key holding fast-entry progress.
FX = "__tgfx"


class TGFastEntry:
    """Tappable sale/purchase entry for Telegram. Holds a router ref for engine access."""

    def __init__(self, router):
        self.router = router

    # ── helpers ──────────────────────────────────────────────────────────

    @property
    def db(self):
        return self.router.db

    @property
    def session(self):
        return self.router.session

    @property
    def tx(self):
        return self.router.transactions

    def _client(self):
        """The Telegram client (from the running bot)."""
        try:
            from main import get_bot
            return get_bot().get_client("telegram")
        except Exception as e:
            logger.warning(f"tg_fastentry: no telegram client: {e}")
            return None

    def _bare(self, phone_number: str) -> str:
        from services.messaging_client import bare_recipient_id
        return bare_recipient_id(phone_number)

    def _get_fx(self, phone_number: str) -> dict:
        return (self.session.get(phone_number).get("context", {}) or {}).get(FX, {}) or {}

    def _save_fx(self, phone_number: str, fx: dict):
        """Persist fast-entry progress, keeping state TG_FASTENTRY."""
        session = self.session.get(phone_number)
        context = dict(session.get("context", {}) or {})
        context[FX] = fx
        self.session.save(phone_number, states.TG_FASTENTRY, context)

    def _render(self, phone_number: str, fx: dict, text: str, keyboard: list):
        """Send (first screen) or edit (subsequent) the single fast-entry message."""
        client = self._client()
        if client is None:
            return
        to = self._bare(phone_number)
        mid = fx.get("msg_id")
        if mid:
            client.edit_message_text(to, mid, text, keyboard=keyboard)
        else:
            mid = client.send_and_get_id(to, text, keyboard=keyboard)
            fx["msg_id"] = mid
        self._save_fx(phone_number, fx)

    # ── entry ────────────────────────────────────────────────────────────

    def start(self, phone_number: str, tx_type: str, products: dict,
              is_service_job: bool = False) -> list:
        """Begin fast-entry. Sends the product screen and stashes state.

        Returns [] because this flow manages its own single message (the engine's
        send loop must not emit anything extra).
        """
        rows = self._product_rows(phone_number, tx_type)
        fx = {
            "step": "product",
            "tx_type": tx_type,
            "is_service_job": is_service_job,
            "page": 0,
            "rows": rows,           # cached catalog rows for paging (best-effort)
        }
        verb = "sell" if tx_type == "sale" else "buy"
        if not rows:
            # No catalog — jump straight to the amount step with a generic item.
            fx["step"] = "amount"
            fx["product_name"] = "Item"
            self._render(phone_number, fx,
                         f"💰 *Record {tx_type}*\n\nHow much?",
                         tg_ui.amount_keyboard(include_back=False))
            return []

        keyboard = tg_ui.product_grid(rows, page=0)
        self._render(phone_number, fx,
                     f"🧾 *Record {tx_type}*\n\nWhat did you {verb}?",
                     keyboard)
        return []

    def _product_rows(self, phone_number: str, tx_type: str) -> list:
        """Reuse the engine's catalog list builders (same data WhatsApp uses)."""
        try:
            cat = self.router.catalog
            user = self.db.get_user(phone_number) or {}
            industry = user.get("industry_class", user.get("business_type", "trading"))
            if tx_type == "purchase" and industry in ("manufacturing", "hybrid"):
                return cat.get_materials_list_for_purchase(phone_number) or []
            if tx_type == "sale" and industry == "services":
                return cat.get_services_list_for_recording(phone_number) or []
            return cat.get_product_list_for_recording(phone_number) or []
        except Exception as e:
            logger.warning(f"tg_fastentry: catalog rows failed: {e}")
            return []

    # ── callback handling (__tgfx__:*) ──────────────────────────────────

    def handle_callback(self, phone_number: str, action: str, value: str = "") -> list:
        """Handle a fast-entry inline tap. Returns [] (self-managed message)."""
        fx = self._get_fx(phone_number)
        if not fx:
            # Stale tap (session expired) — silently ignore.
            return []

        if action == "cancel":
            self.session.reset(phone_number)
            client = self._client()
            if client and fx.get("msg_id"):
                client.edit_message_text(self._bare(phone_number), fx["msg_id"],
                                         "❌ Cancelled.", keyboard=[])
            return []

        if action == "noop":
            return []

        if action == "ppage":
            fx["page"] = int(value) if value.isdigit() else 0
            self._render(phone_number, fx, self._product_prompt(fx),
                         tg_ui.product_grid(fx.get("rows", []), page=fx["page"]))
            return []

        if action == "other":
            # Bail out to the existing free-text guided flow (engine-owned).
            self.session.reset(phone_number)
            return self.router._start_freetext_guided(
                phone_number, fx.get("tx_type", "sale"), fx.get("is_service_job", False)
            )

        if action == "prod":
            return self._pick_product(phone_number, fx, value)

        if action == "amt":
            amount = int(value) if value.isdigit() else 0
            return self._set_amount(phone_number, fx, amount)

        if action == "custom":
            fx["step"] = "await_custom_amount"
            self._save_fx(phone_number, fx)
            client = self._client()
            if client and fx.get("msg_id"):
                client.edit_message_text(
                    self._bare(phone_number), fx["msg_id"],
                    "✏️ Type the amount (e.g. 4500, 5k):", keyboard=[])
            return []

        if action == "pay":
            return self._set_payment(phone_number, fx, value)

        if action == "back":
            return self._go_back(phone_number, fx)

        return []

    def handle_text(self, phone_number: str, text: str) -> list:
        """Handle a typed value while in fast-entry (custom amount)."""
        fx = self._get_fx(phone_number)
        if not fx:
            self.session.reset(phone_number)
            return []
        if fx.get("step") == "await_custom_amount":
            amount = parse_amount(text)
            if not amount:
                client = self._client()
                if client and fx.get("msg_id"):
                    client.edit_message_text(
                        self._bare(phone_number), fx["msg_id"],
                        "💰 That didn't look like an amount. Type e.g. 4500 or 5k:",
                        keyboard=[])
                return []
            return self._set_amount(phone_number, fx, int(amount))
        # Unexpected text — ignore gracefully.
        return []

    # ── step transitions ─────────────────────────────────────────────────

    def _product_prompt(self, fx: dict) -> str:
        verb = "sell" if fx.get("tx_type") == "sale" else "buy"
        return f"🧾 *Record {fx.get('tx_type')}*\n\nWhat did you {verb}?"

    def _pick_product(self, phone_number: str, fx: dict, product_key: str) -> list:
        """Product chosen → resolve its name, move to amount step."""
        name = product_key
        try:
            for r in fx.get("rows", []):
                if r.get("id") == product_key:
                    name = r.get("title", product_key)
                    break
        except Exception:
            pass
        # Strip a leading emoji/space from the display title for a clean description.
        fx["product_key"] = product_key
        fx["product_name"] = name.strip()
        fx["step"] = "amount"
        self._render(phone_number, fx,
                     f"📦 *{fx['product_name']}*\n\nHow much?",
                     tg_ui.amount_keyboard())
        return []

    def _set_amount(self, phone_number: str, fx: dict, amount: int) -> list:
        """Amount chosen → move to payment step."""
        fx["amount"] = int(amount)
        fx["step"] = "payment"
        name = fx.get("product_name", "Item")
        self._render(
            phone_number, fx,
            f"📦 *{name}*\n💰 {format_amount(amount)}\n\nHow was it paid?",
            tg_ui.payment_keyboard(),
        )
        return []

    def _set_payment(self, phone_number: str, fx: dict, method: str) -> list:
        """Payment chosen → assemble tx_data and hand off to the engine confirm card."""
        amount = fx.get("amount", 0)
        tx_type = fx.get("tx_type", "sale")
        name = fx.get("product_name", "Item")
        has_credit = method == "credit"

        tx_data = {
            "amount": int(amount),
            "type": tx_type,
            "description": name,
            "category": "Sales & Income" if tx_type == "sale" else "Goods & Stock",
            "vendor": "",
            "quantity": "",
            "is_service_job": fx.get("is_service_job", False),
            "payment_method": method if method != "credit" else "credit",
            "has_credit": has_credit,
        }
        # Link the catalog product so stock/cost updates target the right entry.
        if fx.get("product_key") and fx["product_key"] != "catrec___other__":
            tx_data["catalog_product"] = fx["product_key"]
            tx_data["catalog_product_name"] = name

        # Clear the editable card (the engine sends its own confirmation next).
        client = self._client()
        if client and fx.get("msg_id"):
            client.edit_message_text(
                self._bare(phone_number), fx["msg_id"],
                f"📦 *{name}* — {format_amount(amount)}\n_Confirming…_", keyboard=[])

        # Hand off: _build_confirmation sets AWAITING_CONFIRMATION; from there the
        # existing confirm → payment → _save_transaction chain runs unchanged.
        # It also stores pending_transaction in the session context.
        return self.tx._build_confirmation(tx_data, has_credit=has_credit)

    def _go_back(self, phone_number: str, fx: dict) -> list:
        """Step back one screen."""
        step = fx.get("step")
        if step in ("amount", "await_custom_amount"):
            # back to product (if we had a product step) else cancel
            if fx.get("rows"):
                fx["step"] = "product"
                self._render(phone_number, fx, self._product_prompt(fx),
                             tg_ui.product_grid(fx.get("rows", []), page=fx.get("page", 0)))
                return []
            self.session.reset(phone_number)
            return []
        if step == "payment":
            fx["step"] = "amount"
            name = fx.get("product_name", "Item")
            self._render(phone_number, fx, f"📦 *{name}*\n\nHow much?",
                         tg_ui.amount_keyboard())
            return []
        return []
