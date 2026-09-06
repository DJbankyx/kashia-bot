# src/features/tg_fastentry.py
"""
Telegram "One Tidy Box" fast-entry — an app-like, tap-first sale/purchase flow.

Telegram ONLY. WhatsApp continues to use the existing guided/catalog flows
unchanged; this is gated behind the `tg:` user-id namespace at the router seam.

Design (see docs/TG_SALE_FLOW_PLAN.md):
- ONE message, edited in place through the whole flow, instead of a stream of
  new messages:
      item → (quantity, counted stock only) → price-each / total → payment
           → quick in-place confirm (Save/Edit/Cancel) → hand to engine save.
- Presentation only. ALL business logic (validation, catalog linkage, saving,
  stock, CRM, landing cost, credit/deposit, industry follow-ups) is REUSED from
  the engine: we assemble a `tx_data` dict and call the engine's payment/save
  path (`TransactionHandler._save_transaction`), after which the normal
  side-effects + industry follow-up questions run untouched.
- Industry wording/defaults come from the industry class' `fastentry_spec()`
  (base.py + per-industry overrides), never hardcoded here.

Pricing rule (consistency, no mixing):
- If the flow asked "how many" (counted stock) → ask PRICE EACH; amount =
  quantity × price_each; unit_cost = price_each.
- If it did NOT ask "how many" (service/one-off job) → ask the TOTAL amount.

Callback actions (from utils.tg_ui, prefix "__tgfx__"):
  prod:<id>  ppage:<n>  other                (product step)
  qty:<int>  qtymore                          (quantity step)
  amt:<int>  custom                           (price/total step)
  pay:<method>                                (payment step)
  save  edit  cancel  undo  new               (confirm / receipt)
  back                                        (navigation)
"""

import logging

from core import states
from utils.parser import parse_amount
from utils import tg_ui
from utils.whatsapp_ui import format_amount

logger = logging.getLogger(__name__)

# Session context key holding fast-entry progress.
FX = "__tgfx"

# Product-row id prefix used by the catalog list builders (id = "catrec_<key>").
_CATREC_PREFIX = "catrec_"


class TGFastEntry:
    """Tappable sale/purchase entry for Telegram. Holds a router ref for engine access."""

    def __init__(self, router):
        self.router = router

    # ── engine accessors ─────────────────────────────────────────────────

    @property
    def db(self):
        return self.router.db

    @property
    def session(self):
        return self.router.session

    @property
    def tx(self):
        return self.router.transactions

    @property
    def catalog(self):
        return self.router.catalog

    def _industry(self, phone_number: str):
        return self.router._get_industry_handler(phone_number)

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

    # ── session helpers ──────────────────────────────────────────────────

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

    def _edit_plain(self, phone_number: str, fx: dict, text: str):
        """Edit the single message to plain text (no keyboard)."""
        client = self._client()
        if client and fx.get("msg_id"):
            client.edit_message_text(self._bare(phone_number), fx["msg_id"], text, keyboard=[])

    # ── entry ────────────────────────────────────────────────────────────

    def start(self, phone_number: str, tx_type: str, products: dict,
              is_service_job: bool = False, preset_vendor: str = "") -> list:
        """Begin fast-entry. Sends the product screen and stashes state.

        Returns [] because this flow manages its own single message (the engine's
        send loop must not emit anything extra).
        """
        spec = self._industry(phone_number).fastentry_spec(tx_type, is_service=is_service_job)
        fx = {
            "step": "product",
            "tx_type": tx_type,
            "is_service": bool(spec.get("is_service")),
            "spec": spec,
            "page": 0,
            "rows": [],
            "msg_id": None,
        }
        # CRM "record sale/purchase to X" pre-fills the counterparty so the
        # "Who?" step is skipped later.
        if preset_vendor:
            fx["vendor"] = preset_vendor
            fx["vendor_preset"] = True

        # ── Expense: no catalog item, no quantity. Ask "what for?" (typed). ──
        if tx_type == "expense":
            fx["step"] = "await_expense_desc"
            self._render(phone_number, fx,
                         f"{self._header(fx)}\n{spec.get('item_prompt', 'What was it for?')}",
                         [])
            return []

        rows = self._product_rows(phone_number, tx_type)
        fx["rows"] = rows

        if not rows:
            # No catalog — go straight to price/total with a generic item.
            fx["product_name"] = "Item"
            fx["product_key"] = ""
            return self._go_to_price_or_qty(phone_number, fx, first_screen=True)

        self._render(phone_number, fx,
                     self._header(fx) + "\n" + spec.get("item_prompt", "What?"),
                     tg_ui.product_grid(rows, page=0))
        return []

    def _product_rows(self, phone_number: str, tx_type: str) -> list:
        """Reuse the engine's catalog list builders (same data WhatsApp uses)."""
        try:
            cat = self.catalog
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

    # ── header / summary line ────────────────────────────────────────────

    def _header(self, fx: dict) -> str:
        """The persistent title line for the card."""
        spec = fx.get("spec", {})
        return f"*{spec.get('title', 'Record')}*"

    def _summary_line(self, fx: dict) -> str:
        """A one-line running summary of what's chosen so far (item ×qty · ₦amount)."""
        name = fx.get("product_name", "")
        parts = []
        if name:
            qty = fx.get("quantity")
            unit = fx.get("unit", "")
            if qty and not fx.get("is_service"):
                if unit:
                    parts.append(f"{name} {int(qty):,} {unit}")
                else:
                    parts.append(f"{name} ×{int(qty):,}")
            else:
                parts.append(name)
        amount = fx.get("amount")
        if amount:
            parts.append(format_amount(amount))
        return " · ".join(parts)

    def _quantity_str(self, fx: dict) -> str:
        """Build the quantity value sent to the engine. Includes the unit when
        one was chosen (so update_stock/_apply_conversion converts to base)."""
        qty = fx.get("quantity")
        if not qty:
            return ""
        unit = fx.get("unit", "")
        return f"{int(qty)} {unit}" if unit else str(int(qty))

    # ── callback handling (__tgfx__:*) ──────────────────────────────────

    def handle_callback(self, phone_number: str, action: str, value: str = "") -> list:
        """Handle a fast-entry inline tap. Returns [] (self-managed message) unless
        handing back to the engine (Save)."""
        fx = self._get_fx(phone_number)

        # Actions that don't need existing fx state.
        if action == "cancel":
            self._cancel(phone_number, fx)
            return []
        if action == "new":
            # Start a fresh sale of the same tx_type.
            self.session.reset(phone_number)
            return self.router._start_guided_recording(
                phone_number, "record_sale" if (fx.get("tx_type") == "sale") else "record_purchase")

        if not fx:
            # Stale tap (session expired) — silently ignore.
            return []

        if action == "noop":
            return []

        if action == "ppage":
            fx["page"] = int(value) if value.isdigit() else 0
            self._render(phone_number, fx,
                         self._header(fx) + "\n" + fx["spec"].get("item_prompt", "What?"),
                         tg_ui.product_grid(fx.get("rows", []), page=fx["page"]))
            return []

        if action == "other":
            # Bail out to the existing free-text guided flow (engine-owned).
            self.session.reset(phone_number)
            return self.router._start_freetext_guided(
                phone_number, fx.get("tx_type", "sale"), fx.get("is_service", False))

        if action == "prod":
            return self._pick_product(phone_number, fx, value)

        if action == "qty":
            n = int(value) if value.isdigit() else 1
            # Choosing a quantity (incl. on an expense) means it's countable →
            # ask price each next.
            fx["counted_stock"] = True
            return self._set_quantity(phone_number, fx, n)

        if action == "qtymore":
            fx["step"] = "await_custom_qty"
            self._save_fx(phone_number, fx)
            self._edit_plain(phone_number, fx, "🔢 Type the quantity (e.g. 24):")
            return []

        if action == "noqty":
            # Expense lump cost (rent/bills): no quantity → ask the total.
            fx["counted_stock"] = False
            fx.pop("quantity", None)
            fx["step"] = "amount"
            self._render(phone_number, fx,
                         f"{self._header(fx)}\n💸 {fx.get('product_name','Expense')}\n\nHow much? (total)",
                         tg_ui.amount_keyboard())
            return []

        if action == "unit":
            return self._set_unit(phone_number, fx, value)

        if action == "amt":
            amount = int(value) if value.isdigit() else 0
            return self._set_price(phone_number, fx, amount)

        if action == "custom":
            fx["step"] = "await_custom_amount"
            self._save_fx(phone_number, fx)
            label = "price each" if self._uses_quantity(fx) else "amount"
            self._edit_plain(phone_number, fx, f"✏️ Type the {label} (e.g. 4500, 5k):")
            return []

        if action == "pay":
            return self._set_payment(phone_number, fx, value)

        # ── who? step (credit / part payment) ──
        if action == "cust":
            # value = contact_id; resolve its display name.
            name = value
            for cid, nm in self._recent_contacts(phone_number):
                if str(cid) == value:
                    name = nm
                    break
            return self._set_customer(phone_number, fx, name)

        if action == "custtype":
            fx["step"] = "await_customer_name"
            self._save_fx(phone_number, fx)
            who = "buyer" if fx.get("tx_type") == "purchase" else "customer"
            self._edit_plain(phone_number, fx, f"✍️ Type the {who}'s name:")
            return []

        if action == "walkin":
            return self._skip_customer(phone_number, fx)

        if action == "save":
            return self._do_save(phone_number, fx)

        if action == "edit":
            # Jump back to the first step (item) for a quick re-do.
            return self._restart_item(phone_number, fx)

        if action == "back":
            return self._go_back(phone_number, fx)

        return []

    def handle_text(self, phone_number: str, text: str) -> list:
        """Handle a typed value while in fast-entry (custom quantity or amount)."""
        fx = self._get_fx(phone_number)
        if not fx:
            self.session.reset(phone_number)
            return []

        step = fx.get("step")
        if step == "await_custom_qty":
            digits = "".join(c for c in text if c.isdigit())
            n = int(digits) if digits else 0
            if n <= 0:
                self._edit_plain(phone_number, fx, "🔢 That didn't look like a number. Type e.g. 24:")
                return []
            fx["counted_stock"] = True  # a typed quantity means it's countable
            return self._set_quantity(phone_number, fx, n)

        if step == "await_custom_amount":
            amount = parse_amount(text)
            if not amount:
                self._edit_plain(phone_number, fx,
                                 "💰 That didn't look like an amount. Type e.g. 4500 or 5k:")
                return []
            return self._set_price(phone_number, fx, int(amount))

        if step == "await_expense_desc":
            desc = text.strip()
            if not desc:
                self._edit_plain(phone_number, fx,
                                 "💸 What was the expense for? (e.g. transport, rent, airtime)")
                return []
            fx["product_name"] = desc
            fx["product_key"] = ""
            # Expenses can be countable (fuel litres, cartons) or a lump cost
            # (rent, bills). Ask "how many?" with a "just a total" escape.
            fx["step"] = "expense_qty"
            presets = self._qty_presets(phone_number, fx)
            self._render(
                phone_number, fx,
                f"{self._header(fx)}\n💸 {desc}\n\n"
                f"How many? _(or tap “Just a total” for rent, bills, etc.)_",
                tg_ui.quantity_keyboard(presets=presets, include_no_qty=True))
            return []

        if step == "await_deposit":
            return self._set_deposit(phone_number, fx, text)

        if step == "await_customer_name":
            name = text.strip()
            if not name:
                self._edit_plain(phone_number, fx, "✍️ Please type a name:")
                return []
            return self._set_customer(phone_number, fx, name)

        # Unexpected text — ignore gracefully.
        return []

    # ── step: product picked ─────────────────────────────────────────────

    def _pick_product(self, phone_number: str, fx: dict, row_id: str) -> list:
        """Product chosen → resolve its real catalog key + name, decide next step."""
        # Row ids are "catrec_<key>"; strip the prefix to get the catalog key.
        product_key = row_id[len(_CATREC_PREFIX):] if row_id.startswith(_CATREC_PREFIX) else row_id

        # Resolve a clean display name + counted-stock flag from the catalog.
        info = {}
        try:
            info = self.catalog.get_item_info(phone_number, product_key)
        except Exception as e:
            logger.warning(f"tg_fastentry: get_item_info failed: {e}")

        name = info.get("name") or self._row_title(fx, row_id) or product_key
        fx["product_key"] = product_key
        fx["product_name"] = name.strip()
        # Counted stock decides whether we ask quantity — UNLESS the industry
        # spec already declares this a service (services / hybrid-service).
        fx["counted_stock"] = bool(info.get("is_counted_stock", True)) and not fx.get("is_service")

        return self._go_to_price_or_qty(phone_number, fx, first_screen=False)

    def _row_title(self, fx: dict, row_id: str) -> str:
        for r in fx.get("rows", []):
            if r.get("id") == row_id:
                # Strip a leading emoji + space for a clean name.
                t = r.get("title", "")
                return t.split(" ", 1)[1] if " " in t and not t[0].isalnum() else t
        return ""

    def _uses_quantity(self, fx: dict) -> bool:
        """True if this entry asked/should ask quantity (counted stock, non-service)."""
        return bool(fx.get("counted_stock")) and not fx.get("is_service")

    def _qty_presets(self, phone_number: str, fx: dict) -> list:
        """Quantity buttons learned from this item's recent sales/purchases."""
        try:
            return self.catalog.suggest_quantities(
                phone_number, fx.get("product_name", ""), fx.get("tx_type", "sale"))
        except Exception as e:
            logger.warning(f"tg_fastentry: qty suggest failed: {e}")
            return []

    def _price_presets(self, phone_number: str, fx: dict) -> list:
        """Price buttons learned from history + the item's set price."""
        try:
            return self.catalog.suggest_prices(
                phone_number, fx.get("product_name", ""), fx.get("product_key", ""),
                counted_stock=self._uses_quantity(fx), tx_type=fx.get("tx_type", "sale"))
        except Exception as e:
            logger.warning(f"tg_fastentry: price suggest failed: {e}")
            return []

    def _go_to_price_or_qty(self, phone_number: str, fx: dict, first_screen: bool) -> list:
        """After an item is chosen, either ask quantity (counted stock) or go to
        the amount step (services / no catalog)."""
        if self._uses_quantity(fx):
            fx["step"] = "quantity"
            text = f"{self._header(fx)}\n📦 {fx['product_name']}\n\nHow many?"
            presets = self._qty_presets(phone_number, fx)
            self._render(phone_number, fx, text, tg_ui.quantity_keyboard(presets=presets))
            return []
        # No quantity → ask the total amount directly.
        fx["step"] = "amount"
        text = f"{self._header(fx)}\n📦 {fx.get('product_name','Item')}\n\nHow much? (total)"
        presets = self._price_presets(phone_number, fx)
        self._render(phone_number, fx, text, tg_ui.amount_keyboard(presets=presets or None))
        return []

    # ── step: quantity ───────────────────────────────────────────────────

    def _set_quantity(self, phone_number: str, fx: dict, n: int) -> list:
        fx["quantity"] = int(n)
        # 2F: if this item has registered unit conversions (e.g. 1 bag = 20
        # pieces), ask WHICH unit this quantity is in, then convert on save.
        units = self._item_units(phone_number, fx)
        if units.get("has_conversions") and len(units.get("units", [])) > 1:
            fx["step"] = "unit"
            fx["_units"] = units["units"]
            self._save_fx(phone_number, fx)
            text = (f"{self._header(fx)}\n📦 {fx['product_name']} — {n:,}\n\n"
                    f"In what unit?")
            self._render(phone_number, fx, text, tg_ui.unit_keyboard(units["units"]))
            return []
        # No conversions → straight to price (existing behaviour).
        return self._go_to_price(phone_number, fx)

    def _item_units(self, phone_number: str, fx: dict) -> dict:
        try:
            key = fx.get("product_key")
            if key:
                return self.catalog.get_item_units(phone_number, key)
        except Exception as e:
            logger.warning(f"tg_fastentry: get_item_units failed: {e}")
        return {"base": "", "units": [], "has_conversions": False}

    def _set_unit(self, phone_number: str, fx: dict, unit: str) -> list:
        """2F: user picked the unit for the quantity → go to price."""
        fx["unit"] = unit
        return self._go_to_price(phone_number, fx)

    def _go_to_price(self, phone_number: str, fx: dict) -> list:
        fx["step"] = "price"
        n = int(fx.get("quantity", 1) or 1)
        unit = fx.get("unit", "")
        qty_disp = f"{n:,} {unit}" if unit else f"×{n:,}"
        text = (f"{self._header(fx)}\n📦 {fx['product_name']} {qty_disp}\n\n"
                f"Price per {unit}?" if unit else
                f"{self._header(fx)}\n📦 {fx['product_name']} ×{n:,}\n\nPrice each?")
        presets = self._price_presets(phone_number, fx)
        self._render(phone_number, fx, text, tg_ui.amount_keyboard(presets=presets or None))
        return []

    # ── step: price (each) or total ──────────────────────────────────────

    def _set_price(self, phone_number: str, fx: dict, value: int) -> list:
        """Set the price. If quantity was asked, `value` = price each and we
        multiply; otherwise `value` = total amount."""
        if self._uses_quantity(fx):
            qty = int(fx.get("quantity", 1) or 1)
            fx["unit_cost"] = int(value)
            fx["amount"] = int(value) * qty
        else:
            fx["unit_cost"] = None
            fx["amount"] = int(value)
        fx["step"] = "payment"

        if fx.get("tx_type") == "sale":
            text = f"{self._header(fx)}\n{self._summary_line(fx)}\n\nHow were you paid?"
            credit_label = "💳 Credit (owes me)"
        else:
            text = f"{self._header(fx)}\n{self._summary_line(fx)}\n\nHow did you pay?"
            credit_label = "💳 Credit (I owe)"
        self._render(phone_number, fx, text, tg_ui.payment_keyboard(credit_label=credit_label))
        return []

    # ── step: payment ────────────────────────────────────────────────────

    def _set_payment(self, phone_number: str, fx: dict, method: str) -> list:
        """Payment chosen → branch by method.

        - cash / transfer → straight to the in-place confirm (Option B).
        - part            → ask the deposit amount (in-box), then Who?, then confirm.
        - credit          → ask Who? (in-box), then confirm.
        The customer name is collected IN THE BOX (no hand-off to the old text
        step) so credit/part is boxed, confirms cleanly, and records the debt.
        """
        fx["payment_method"] = method
        fx["has_credit"] = method in ("credit", "part")

        if method == "part":
            fx["step"] = "await_deposit"
            self._save_fx(phone_number, fx)
            self._edit_plain(
                phone_number, fx,
                f"{self._header(fx)}\n{self._summary_line(fx)}\n\n"
                f"💰 How much was paid now? (deposit)\n_e.g. 25000, 50k, half_")
            return []

        # If the counterparty was pre-set (CRM "record to X"), skip the who-step.
        if fx.get("vendor_preset") and fx.get("vendor"):
            return self._show_confirm(phone_number, fx)

        if method == "credit":
            return self._ask_customer(phone_number, fx, required=True)

        # cash / transfer → still OFFER a name (feeds the CRM), but it's optional
        # here (Walk-in / Skip is fine). This keeps every path boxed and avoids
        # the old text "who?" prompt for paid-in-full transactions.
        return self._ask_customer(phone_number, fx, required=False)

    def _pay_label(self, fx: dict) -> str:
        method = fx.get("payment_method", "cash")
        base = {
            "cash": "💵 Cash", "transfer": "🏦 Transfer",
            "credit": "💳 On credit", "part": "📝 Part payment",
        }.get(method, method)
        if method == "part" and fx.get("deposit_amount") is not None:
            base += (f" — paid {format_amount(fx['deposit_amount'])}, "
                     f"owes {format_amount(fx.get('balance_owed', 0))}")
        return base

    def _show_confirm(self, phone_number: str, fx: dict) -> list:
        """The single in-place confirm card (Option B)."""
        fx["step"] = "confirm"
        lines = [
            f"{self._header(fx)}",
            "",
            f"{self._summary_line(fx)}",
        ]
        if self._uses_quantity(fx) and fx.get("unit_cost"):
            lines.append(f"   ({fx['quantity']} × {format_amount(fx['unit_cost'])} each)")
        lines.append(f"💳 {self._pay_label(fx)}")
        if fx.get("vendor"):
            who = "Customer" if fx.get("tx_type") == "sale" else "Supplier"
            lines.append(f"👤 {who}: {fx['vendor']}")
        lines += ["", "_Save this?_"]
        self._render(phone_number, fx, "\n".join(lines), tg_ui.confirm_keyboard())
        return []

    # ── step: deposit amount (part payment) ──────────────────────────────

    def _set_deposit(self, phone_number: str, fx: dict, text: str) -> list:
        """Deposit amount entered → compute balance, then ask Who?."""
        total = int(fx.get("amount", 0))
        low = text.lower().strip()
        if low in ("half", "50%"):
            deposit = total // 2
        else:
            deposit = parse_amount(text)
        if not deposit:
            self._edit_plain(
                phone_number, fx,
                f"💰 Enter the deposit amount (e.g. 25000, 50k).\n"
                f"_Total is {format_amount(total)}._")
            return []
        deposit = int(deposit)
        if deposit >= total:
            # Paid in full — treat as a normal cash sale (no debt).
            fx["payment_method"] = "cash"
            fx["has_credit"] = False
            fx.pop("deposit_amount", None)
            fx.pop("balance_owed", None)
            return self._show_confirm(phone_number, fx)
        fx["deposit_amount"] = deposit
        fx["balance_owed"] = total - deposit
        return self._ask_customer(phone_number, fx, required=True)

    # ── step: who? (customer / supplier) ─────────────────────────────────

    def _recent_contacts(self, phone_number: str) -> list:
        """(contact_id, name) of recent/known contacts for quick tap."""
        try:
            contacts = self.db.get_contacts(phone_number, limit=20) or []
            out = []
            for c in contacts:
                name = c.get("name") or ""
                cid = c.get("contact_id") or name.lower().replace(" ", "_")
                if name:
                    out.append((cid, name))
            return out
        except Exception as e:
            logger.warning(f"tg_fastentry: contacts read failed: {e}")
            return []

    def _ask_customer(self, phone_number: str, fx: dict, required: bool = True) -> list:
        """Boxed 'Who?' step.

        `required=True` (credit/part): a debt needs an owner — but Walk-in is
        still offered so the user is never stuck.
        `required=False` (cash/transfer): naming is OPTIONAL (feeds the CRM), and
        Skip records with no name. Either way it's boxed — never the old text step.
        """
        fx["step"] = "who"
        fx["who_required"] = bool(required)
        self._save_fx(phone_number, fx)
        recent = self._recent_contacts(phone_number)
        tt = fx.get("tx_type")
        if tt == "purchase":
            q = "👤 Who did you buy from?"
        elif tt == "expense":
            q = "👤 Who did you pay?"
        else:
            q = "👤 Who did you sell to?"
        if required:
            note = "_Needed to track the debt. Tap a name, type one, or choose Walk-in._"
        else:
            note = "_Optional — tap a name to track this customer, or Skip._"
        text = f"{self._header(fx)}\n{self._summary_line(fx)}\n\n{q}\n{note}"
        self._render(phone_number, fx, text, tg_ui.customer_keyboard(recent=recent))
        return []

    def _set_customer(self, phone_number: str, fx: dict, name: str) -> list:
        """Name chosen/typed → store vendor, go to confirm."""
        fx["vendor"] = name.strip()
        return self._show_confirm(phone_number, fx)

    def _skip_customer(self, phone_number: str, fx: dict) -> list:
        """Walk-in / Skip tapped.

        - Debt path (credit/part, required): use a "Walk-in" bucket so the debt
          still has an owner.
        - Paid-in-full (optional): leave vendor empty → engine saves with no debt
          and no CRM contact (no fake record).
        """
        if fx.get("who_required"):
            fx["vendor"] = "Walk-in"
        else:
            fx["vendor"] = ""
        return self._show_confirm(phone_number, fx)

    # ── confirm → hand to the engine save chain ──────────────────────────

    def _build_tx_data(self, fx: dict) -> dict:
        spec = fx.get("spec", {})
        method = fx.get("payment_method", "cash")
        # For part payment, the engine's credit branch reads payment_method
        # "deposit" + deposit_amount + balance_owed to record only the balance.
        pm = "deposit" if method == "part" else method
        tx_data = {
            "amount": int(fx.get("amount", 0)),
            "type": fx.get("tx_type", "sale"),
            "description": fx.get("product_name", "Item"),
            "category": spec.get("category", "Uncategorized"),
            "vendor": fx.get("vendor", ""),
            # 2F: carry the entered unit so the engine's update_stock/_apply_conversion
            # converts to base (e.g. "20 bags" -> 400 pieces). Plain int if no unit.
            "quantity": self._quantity_str(fx),
            "unit_cost": fx.get("unit_cost"),
            "is_service_job": bool(fx.get("is_service")),
            "payment_method": pm,
            "has_credit": bool(fx.get("has_credit")),
            # The tidy box always runs its own boxed "Who?" step, so the engine
            # must NOT fall back to the old text CRM prompt.
            "_name_handled": True,
        }
        if method == "part":
            tx_data["deposit_amount"] = int(fx.get("deposit_amount", 0))
            tx_data["balance_owed"] = int(fx.get("balance_owed", 0))
        if fx.get("product_key"):
            tx_data["catalog_product"] = fx["product_key"]
            tx_data["catalog_product_name"] = fx.get("product_name", "")
        return tx_data

    def _do_save(self, phone_number: str, fx: dict) -> list:
        """Hand off to the engine. Reuses ALL money/stock/credit/follow-up logic.

        Credit/part already collected the customer name in-box, so tx_data has a
        vendor → _save_transaction routes to _save_credit_transaction, which
        records the correct debt (balance for part, full for credit) and returns
        a clean confirmation. Cash/transfer save straight through.
        """
        tx_data = self._build_tx_data(fx)
        self._edit_plain(phone_number, fx,
                         f"✅ *Saving…*\n{self._summary_line(fx)}")
        self.session.reset(phone_number)
        return self.tx._save_transaction(phone_number, tx_data)

    # ── navigation / lifecycle ───────────────────────────────────────────

    def _restart_item(self, phone_number: str, fx: dict) -> list:
        """Edit → go back to the item picker (fresh choices, same message)."""
        fx["step"] = "product"
        for k in ("product_key", "product_name", "quantity", "unit_cost", "amount",
                  "payment_method", "has_credit", "vendor", "deposit_amount",
                  "balance_owed", "counted_stock", "who_required", "unit", "_units"):
            fx.pop(k, None)
        # Expense restarts at the typed "what for?" step (no catalog picker).
        if fx.get("tx_type") == "expense":
            fx["step"] = "await_expense_desc"
            self._render(phone_number, fx,
                         f"{self._header(fx)}\n{fx['spec'].get('item_prompt', 'What was it for?')}",
                         [])
            return []
        rows = fx.get("rows", [])
        if rows:
            self._render(phone_number, fx,
                         self._header(fx) + "\n" + fx["spec"].get("item_prompt", "What?"),
                         tg_ui.product_grid(rows, page=0))
        else:
            self._go_to_price_or_qty(phone_number, fx, first_screen=True)
        return []

    def _go_back(self, phone_number: str, fx: dict) -> list:
        """Step back one screen."""
        step = fx.get("step")
        if step in ("amount", "quantity", "expense_qty", "await_custom_qty", "await_custom_amount"):
            # expense → back to the "what for?" step; catalog flows → item picker;
            # otherwise nothing to go back to → cancel.
            if fx.get("tx_type") == "expense":
                return self._restart_item(phone_number, fx)
            if fx.get("rows"):
                return self._restart_item(phone_number, fx)
            self._cancel(phone_number, fx)
            return []
        if step == "unit":
            # back to quantity
            fx["step"] = "quantity"
            presets = self._qty_presets(phone_number, fx)
            self._render(phone_number, fx,
                         f"{self._header(fx)}\n📦 {fx['product_name']}\n\nHow many?",
                         tg_ui.quantity_keyboard(presets=presets))
            return []
        if step == "price":
            # back to quantity (expense uses its own qty step with the total escape)
            if fx.get("tx_type") == "expense":
                fx["step"] = "expense_qty"
                presets = self._qty_presets(phone_number, fx)
                self._render(
                    phone_number, fx,
                    f"{self._header(fx)}\n💸 {fx.get('product_name','Expense')}\n\n"
                    f"How many? _(or tap “Just a total”)_",
                    tg_ui.quantity_keyboard(presets=presets, include_no_qty=True))
                return []
            # If a unit was chosen (2F), step back to the unit toggle; else qty.
            if fx.get("unit") and fx.get("_units"):
                fx["step"] = "unit"
                self._render(phone_number, fx,
                             f"{self._header(fx)}\n📦 {fx['product_name']} — {int(fx.get('quantity',1)):,}\n\nIn what unit?",
                             tg_ui.unit_keyboard(fx["_units"]))
                return []
            fx["step"] = "quantity"
            presets = self._qty_presets(phone_number, fx)
            self._render(phone_number, fx,
                         f"{self._header(fx)}\n📦 {fx['product_name']}\n\nHow many?",
                         tg_ui.quantity_keyboard(presets=presets))
            return []
        if step in ("who", "await_customer_name", "await_deposit"):
            # back to the payment picker
            fx["step"] = "payment"
            credit_label = "💳 Credit (owes me)" if fx.get("tx_type") == "sale" else "💳 Credit (I owe)"
            prompt = "How were you paid?" if fx.get("tx_type") == "sale" else "How did you pay?"
            self._render(phone_number, fx,
                         f"{self._header(fx)}\n{self._summary_line(fx)}\n\n{prompt}",
                         tg_ui.payment_keyboard(credit_label=credit_label))
            return []
        if step == "payment":
            # back to price/amount
            presets = self._price_presets(phone_number, fx)
            if self._uses_quantity(fx):
                fx["step"] = "price"
                self._render(phone_number, fx,
                             f"{self._header(fx)}\n📦 {fx['product_name']} ×{int(fx.get('quantity',1)):,}\n\nPrice each?",
                             tg_ui.amount_keyboard(presets=presets or None))
            else:
                fx["step"] = "amount"
                self._render(phone_number, fx,
                             f"{self._header(fx)}\n📦 {fx.get('product_name','Item')}\n\nHow much? (total)",
                             tg_ui.amount_keyboard(presets=presets or None))
            return []
        if step == "confirm":
            # back to the payment picker (re-choose method)
            fx["step"] = "payment"
            credit_label = "💳 Credit (owes me)" if fx.get("tx_type") == "sale" else "💳 Credit (I owe)"
            prompt = "How were you paid?" if fx.get("tx_type") == "sale" else "How did you pay?"
            self._render(phone_number, fx,
                         f"{self._header(fx)}\n{self._summary_line(fx)}\n\n{prompt}",
                         tg_ui.payment_keyboard(credit_label=credit_label))
            return []
        return []

    def _cancel(self, phone_number: str, fx: dict):
        self.session.reset(phone_number)
        if fx.get("msg_id"):
            self._edit_plain(phone_number, fx, "❌ Cancelled.")

    # NOTE: Undo-after-save is a planned follow-up. Wiring it means capturing the
    # saved tx_id and adding an Undo button onto the engine's post-save receipt,
    # which touches the shared save path (WhatsApp too). Deferred so we don't
    # destabilize the money path while validating the front-half tidy box.
