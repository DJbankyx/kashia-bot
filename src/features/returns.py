"""Returns / Refunds — Telegram tap-first UI (build #4, R3).

A thin PRESENTATION layer over the shared engine. All the money logic lives in
TransactionHandler.record_return (R1) and the accounting nets returns in R2 —
this module only collects the user's choices (which original sale/purchase, how
many units) and renders results. WhatsApp is intentionally NOT wired here yet
(Telegram-first); on WhatsApp show_menu falls back to a short "coming soon"
note, so nothing existing breaks.

Flow (Telegram):
  show_menu → list recent sales & purchases (tappable)
    → ret_pick_<tx_id> → show the original + a quantity picker (full / partial)
      → ret_qty_<tx_id>_<n> (full or a stepper choice) → record_return → result card
  A "type a number" partial path uses RETURN_RECORDING state.

Callback namespace: everything starts with `return_` so the engine's
ButtonDispatcher routes it here (see core/button_dispatcher.py). We keep tx_ids
in button ids (short + safe) and slice to 60 bytes defensively.
"""

import logging

from core import states
from utils.whatsapp_ui import text_response, button_response, list_response, format_amount

logger = logging.getLogger(__name__)

_PICK = "return_pick_"     # return_pick_<tx_id>
_QTY = "return_qty_"       # return_qty_<tx_id>_<n>  (or _full)
_MENU = "menu_returns"


class ReturnsHandler:
    """Tap-first Returns/Refunds board (Telegram). Composes the engine's
    record_return; never forks money logic."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database
        self.router = None  # set by KashiaBot.__init__ (for transactions.record_return)

    # ── platform gate (WhatsApp untouched) ──
    def _is_telegram(self, phone_number: str) -> bool:
        try:
            from services.messaging_client import platform_for_user
            return platform_for_user(phone_number) == "telegram"
        except Exception:
            return False

    def _tx_handler(self):
        """The shared TransactionHandler that owns record_return."""
        return getattr(self.router, "transactions", None) if self.router else None

    # ── helpers ──
    @staticmethod
    def _qty_of(tx) -> int:
        import re
        m = re.match(r"^\s*(\d+)", str(tx.get("quantity", "1") or "1"))
        return int(m.group(1)) if m else 1

    def _recent_returnable(self, phone_number: str, limit: int = 40):
        """Recent sales + purchases that still have units left to return.
        Returns a list of (tx, remaining) newest-first."""
        txns = self.db.get_transactions(phone_number, limit=limit) or []
        tx = self._tx_handler()
        out = []
        for t in txns:
            if t.get("type") not in ("sale", "purchase"):
                continue
            tx_id = t.get("transaction_id", "")
            orig_qty = self._qty_of(t)
            already = tx.returned_qty_for(phone_number, tx_id) if tx else 0
            remaining = orig_qty - already
            if remaining > 0:
                out.append((t, remaining))
        return out

    # ═══════════════════════════ entry ═══════════════════════════
    def show_menu(self, phone_number: str) -> list:
        """List recent returnable sales/purchases as tappable rows."""
        if not self._is_telegram(phone_number):
            return [text_response(
                "↩️ *Returns* are available in the Telegram app for now.\n"
                "_On WhatsApp, record a correction manually for the moment._"
            )]

        tx = self._tx_handler()
        if tx is None:
            return [text_response("⚠️ Returns aren't available right now — try again shortly.")]

        items = self._recent_returnable(phone_number)
        if not items:
            return [text_response(
                "↩️ *Record a Return*\n\n"
                "No recent sales or purchases to return.\n"
                "_Returns reverse a sale (goods come back, refund out) or a "
                "purchase (goods go back, money in)._"
            )]

        rows = []
        rows.append({"id": "return_noop", "title": "─── Pick what to return ───"})
        for t, remaining in items[:20]:
            tx_id = t.get("transaction_id", "")
            kind = "🧾 Sale" if t.get("type") == "sale" else "📦 Purchase"
            name = (t.get("item_name") or t.get("description") or "Item").strip()
            amt = format_amount(t.get("amount", 0))
            date = str(t.get("date", ""))[:10]
            title = f"{kind}: {name} · {amt}"
            if remaining > 1:
                title += f" ({remaining} left)"
            rows.append({
                "id": f"{_PICK}{tx_id}"[:60],
                "title": title[:60],
                "description": date,
            })

        return [list_response(
            header="↩️ Record a Return",
            body="Tap the original sale or purchase you're reversing.",
            button_text="Pick",
            sections=[{"title": "", "rows": rows}],
            no_paginate=True,
        )]

    # ═══════════════════════════ buttons ═══════════════════════════
    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        bid = button_id.strip()

        if bid == _MENU or bid == "return_menu":
            self.session.reset(phone_number)
            return self.show_menu(phone_number)

        if bid == "return_noop":
            return []

        if bid == "return_back":
            self.session.reset(phone_number)
            return self.show_menu(phone_number)

        # Picked an original → show it + quantity choices.
        if bid.startswith(_PICK):
            return self._pick_original(phone_number, bid[len(_PICK):])

        # Chose a quantity (full or a specific number) → record.
        if bid.startswith(_QTY):
            rest = bid[len(_QTY):]
            # rest = "<tx_id>_<n>"  (n = "full" or an integer)
            tx_id, _, qty_str = rest.rpartition("_")
            if qty_str == "full":
                return self._do_return(phone_number, tx_id, None)
            if qty_str == "custom":
                return self._ask_custom_qty(phone_number, tx_id)
            try:
                return self._do_return(phone_number, tx_id, int(qty_str))
            except ValueError:
                return self._pick_original(phone_number, tx_id)

        return self.show_menu(phone_number)

    def _pick_original(self, phone_number: str, tx_id: str) -> list:
        original = self.db.get_transaction(phone_number, tx_id)
        if not original or original.get("type") not in ("sale", "purchase"):
            return [text_response("⚠️ That original couldn't be found.")] + \
                self.show_menu(phone_number)

        tx = self._tx_handler()
        orig_qty = self._qty_of(original)
        already = tx.returned_qty_for(phone_number, tx_id) if tx else 0
        remaining = orig_qty - already
        if remaining <= 0:
            return [text_response(
                f"✅ All {orig_qty} already returned on this one — nothing left."
            )] + self.show_menu(phone_number)

        is_sale = original.get("type") == "sale"
        name = (original.get("item_name") or original.get("description") or "Item").strip()
        amt = int(original.get("amount") or 0)
        who = (original.get("vendor") or "").strip()
        pm = (original.get("payment_method") or "").lower()
        on_credit = pm == "credit" or bool(original.get("has_credit"))

        verb = "refund the customer" if is_sale else "get money back from the supplier"
        if on_credit:
            verb = ("cancel what they owe you" if is_sale
                    else "cancel what you owe the supplier")

        body = [
            f"{'🧾 Sale' if is_sale else '📦 Purchase'}: *{name}*",
            f"💵 {format_amount(amt)}" + (f" · {who}" if who else ""),
            f"📦 Qty: {orig_qty}" + (f" ({remaining} still returnable)" if already else ""),
            "",
            f"Returning will add the goods back to stock and *{verb}*.",
            "",
            "How many are coming back?",
        ]

        # Quantity choices: full, then a few steppers up to remaining.
        rows = [{
            "id": f"{_QTY}{tx_id}_full"[:60],
            "title": f"↩️ All {remaining}" if remaining == orig_qty else f"↩️ Remaining {remaining}",
        }]
        # Offer 1..min(remaining-1, 5) as quick partial taps.
        for n in range(1, min(remaining, 6)):
            rows.append({"id": f"{_QTY}{tx_id}_{n}"[:60], "title": f"{n}"})
        if remaining > 6:
            rows.append({"id": f"{_QTY}{tx_id}_custom"[:60], "title": "✏️ Type a number"})
        rows.append({"id": "return_back", "title": "⬅️ Back"})

        return [list_response(
            header="↩️ Confirm return",
            body="\n".join(body),
            button_text="Quantity",
            sections=[{"title": "", "rows": rows}],
            no_paginate=True,
        )]

    def _ask_custom_qty(self, phone_number: str, tx_id: str) -> list:
        self.session.save(phone_number, states.RETURN_RECORDING, {
            "return_tx_id": tx_id,
        })
        return [text_response("✏️ How many units are being returned? (Type a number)")]

    # ── numeric text step (RETURN_RECORDING) ──
    def handle(self, phone_number: str, text: str, session: dict) -> list:
        context = session.get("context", {})
        tx_id = context.get("return_tx_id", "")
        import re
        m = re.match(r"^\s*(\d+)", str(text).strip())
        if not m:
            return [text_response("Please type just the number of units to return (e.g. 2).")]
        qty = int(m.group(1))
        self.session.reset(phone_number)
        if not tx_id:
            return self.show_menu(phone_number)
        return self._do_return(phone_number, tx_id, qty)

    # ═══════════════════════════ record ═══════════════════════════
    def _do_return(self, phone_number: str, tx_id: str, qty) -> list:
        tx = self._tx_handler()
        if tx is None:
            return [text_response("⚠️ Returns aren't available right now.")]

        result = tx.record_return(phone_number, tx_id, qty)
        if not result.get("ok"):
            err = result.get("error", "couldn't record the return")
            return [text_response(f"⚠️ {err}.")] + self.show_menu(phone_number)

        is_sale = result.get("type") == "sale_return"
        amt = format_amount(result.get("amount", 0))
        n = result.get("quantity", 0)
        orig_qty = result.get("original_qty", 0)
        mode = result.get("refund_mode", "cash")

        if is_sale:
            head = f"↩️ *Sale return recorded* · {amt}"
            stock_line = f"📦 {n} unit(s) added back to stock."
            money_line = ("💳 Cancelled the balance the customer owed you."
                          if mode == "debt_cancelled"
                          else "💵 Refund to the customer (cash out).")
        else:
            head = f"↩️ *Purchase return recorded* · {amt}"
            stock_line = f"📦 {n} unit(s) removed from stock (sent back)."
            money_line = ("💳 Cancelled the balance you owed the supplier."
                          if mode == "debt_cancelled"
                          else "💵 Money back from the supplier (cash in).")

        lines = [head]
        if n and orig_qty and n < orig_qty:
            lines.append(f"_(partial: {n} of {orig_qty})_")
        lines += [stock_line, money_line]
        if not result.get("stock_matched"):
            lines.append("_Note: no matching catalog item, so stock wasn't adjusted._")

        return [button_response(
            "\n".join(lines),
            [
                {"id": "menu_returns", "title": "↩️ Another return"},
                {"id": "menu_home", "title": "☰ Menu"},
            ]
        )]
