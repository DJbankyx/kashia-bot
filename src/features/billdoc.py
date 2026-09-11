"""Bill a customer — multi-item SINGLE document (Option A, build #4 follow-on).

Lets the owner pick a customer, toggle SEVERAL of that customer's sales, and
generate ONE invoice or receipt covering all of them (e.g. two car sales → one
invoice with two proper line items — fixes the "Items: 1 / qty 2 Honda" pain).

Presentation only: it just assembles a LIST of transaction_ids and hands them to
the existing shared generators (pdf_generator.handle_multi_invoice_request /
handle_multi_receipt_request) via the __GEN_INVOICE__ / __GEN_RECEIPT__ markers
(now tx_ids-list aware). NO new document/accounting logic.

Telegram tap-first (billdoc_* callbacks). WhatsApp gets a short "in the app"
note — the classic per-sale gen_invoice/gen_receipt buttons still work there.

Callback namespace (all under billdoc_ so the engine routes here):
  billdoc_cust_<contact_id>   pick a customer
  billdoc_tog_<tx_id>         toggle a sale in/out of the selection
  billdoc_all                 select all shown
  billdoc_clear               clear the selection
  billdoc_inv / billdoc_rcpt  generate invoice / receipt from the selection
  billdoc_back                back to the customer list
  billdoc_noop                inert divider
"""

import logging

from core import states
from utils.whatsapp_ui import text_response, button_response, list_response, format_amount

logger = logging.getLogger(__name__)

_CUST = "billdoc_cust_"
_TOG = "billdoc_tog_"


class BillDocHandler:
    """Tap-first 'bill a customer' multi-select → one invoice/receipt."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database
        self.router = None  # set by KashiaBot (not strictly needed, kept uniform)

    def _is_telegram(self, phone_number: str) -> bool:
        try:
            from services.messaging_client import platform_for_user
            return platform_for_user(phone_number) == "telegram"
        except Exception:
            return False

    # ── data helpers ─────────────────────────────────────────────
    @staticmethod
    def _cust_key(name: str) -> str:
        return (name or "").strip().lower().replace(" ", "_")

    def _recent_sales(self, phone_number: str, limit: int = 80):
        """Recent real sales (type == sale), newest-first, with a customer name."""
        txns = self.db.get_transactions(phone_number, limit=limit) or []
        out = []
        for t in txns:
            if t.get("type") != "sale":
                continue
            out.append(t)
        return out

    def _customers_with_sales(self, phone_number: str):
        """Group recent sales by customer (vendor field). Returns a list of
        {key, name, count, total} sorted by most-recent activity (list order)."""
        sales = self._recent_sales(phone_number)
        groups = {}
        order = []
        for t in sales:
            name = (t.get("vendor") or "").strip()
            if not name or name.lower() in ("unknown", "walk-in", "walk in", "customer"):
                name = "Walk-in / Unnamed"
            key = self._cust_key(name)
            if key not in groups:
                groups[key] = {"key": key, "name": name, "count": 0, "total": 0}
                order.append(key)
            groups[key]["count"] += 1
            groups[key]["total"] += int(t.get("amount", 0) or 0)
        return [groups[k] for k in order]

    def _sales_for_customer(self, phone_number: str, cust_key: str):
        """The recent sales belonging to a customer key (newest-first)."""
        out = []
        for t in self._recent_sales(phone_number):
            name = (t.get("vendor") or "").strip() or "Walk-in / Unnamed"
            if self._cust_key(name if name.lower() not in
                              ("unknown", "walk-in", "walk in", "customer")
                              else "Walk-in / Unnamed") == cust_key:
                out.append(t)
        return out

    # ═══════════════════════════ entry ═══════════════════════════
    def show_menu(self, phone_number: str) -> list:
        """List customers who have recent sales → pick one to bill."""
        if not self._is_telegram(phone_number):
            return [text_response(
                "🧾 *Bill a customer* (combine several sales into one invoice) is "
                "in the Telegram app for now.\n_On WhatsApp, generate a document "
                "per sale as usual._"
            )]

        customers = self._customers_with_sales(phone_number)
        if not customers:
            return [text_response(
                "🧾 *Bill a customer*\n\nNo recent sales to bill yet.\n"
                "_Record a few sales, then come back to combine them into one "
                "invoice or receipt._"
            )]

        rows = [{"id": "billdoc_noop", "title": "─── Pick a customer ───"}]
        for c in customers[:20]:
            rows.append({
                "id": f"{_CUST}{c['key']}"[:60],
                "title": f"{c['name']} · {c['count']} sale(s) · {format_amount(c['total'])}"[:60],
            })

        return [list_response(
            header="🧾 Bill a customer",
            body="Combine several sales to one customer into a single invoice or "
                 "receipt. Tap a customer to choose which sales to include.",
            button_text="Customer",
            sections=[{"title": "", "rows": rows}],
            no_paginate=True,
        )]

    # ═══════════════════════════ buttons ═══════════════════════════
    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        bid = button_id.strip()

        if bid in ("menu_billdoc", "billdoc_menu"):
            self.session.reset(phone_number)
            return self.show_menu(phone_number)

        if bid == "billdoc_noop":
            return []

        if bid == "billdoc_back":
            self.session.reset(phone_number)
            return self.show_menu(phone_number)

        if bid.startswith(_CUST):
            return self._open_customer(phone_number, bid[len(_CUST):])

        # From here on we need an active selection session.
        context = session.get("context", {}) if session else {}
        cust_key = context.get("billdoc_cust", "")
        selected = set(context.get("billdoc_selected", []))

        if bid.startswith(_TOG):
            tx_id = bid[len(_TOG):]
            if tx_id in selected:
                selected.discard(tx_id)
            else:
                selected.add(tx_id)
            return self._render_selection(phone_number, cust_key, selected)

        if bid == "billdoc_all":
            sales = self._sales_for_customer(phone_number, cust_key)
            selected = {t.get("transaction_id", "") for t in sales if t.get("transaction_id")}
            return self._render_selection(phone_number, cust_key, selected)

        if bid == "billdoc_clear":
            return self._render_selection(phone_number, cust_key, set())

        if bid in ("billdoc_inv", "billdoc_rcpt"):
            return self._generate(phone_number, cust_key, selected,
                                  kind="invoice" if bid == "billdoc_inv" else "receipt")

        return self.show_menu(phone_number)

    def _open_customer(self, phone_number: str, cust_key: str) -> list:
        # Start a fresh selection = ALL of this customer's sales (sensible default;
        # user can deselect). Saved in session state.
        sales = self._sales_for_customer(phone_number, cust_key)
        if not sales:
            return [text_response("No sales found for that customer.")] + \
                self.show_menu(phone_number)
        selected = {t.get("transaction_id", "") for t in sales if t.get("transaction_id")}
        return self._render_selection(phone_number, cust_key, selected)

    def _render_selection(self, phone_number: str, cust_key: str, selected: set) -> list:
        sales = self._sales_for_customer(phone_number, cust_key)
        if not sales:
            self.session.reset(phone_number)
            return [text_response("No sales found for that customer.")] + \
                self.show_menu(phone_number)

        # Persist the selection set + customer for the next tap.
        self.session.save(phone_number, states.BILLDOC_SELECT, {
            "billdoc_cust": cust_key,
            "billdoc_selected": list(selected),
        })

        cust_name = (sales[0].get("vendor") or "").strip() or "Walk-in / Unnamed"
        sel_total = sum(int(t.get("amount", 0) or 0) for t in sales
                        if t.get("transaction_id") in selected)

        from services.pdf_generator import PDFGenerator  # for the shared label
        try:
            labeller = self.router.pdf_generator if getattr(self, "router", None) \
                and getattr(self.router, "pdf_generator", None) else None
        except Exception:
            labeller = None

        rows = [{"id": "billdoc_noop",
                 "title": f"─── {cust_name} · tap to include ───"[:60]}]
        for t in sales[:20]:
            tx_id = t.get("transaction_id", "")
            checked = "✅" if tx_id in selected else "⬜"
            desc = self._label(t, labeller)
            amt = format_amount(t.get("amount", 0))
            date = str(t.get("date", ""))[:10]
            rows.append({
                "id": f"{_TOG}{tx_id}"[:60],
                "title": f"{checked} {desc} · {amt}"[:60],
                "description": date,
            })

        n = len(selected)
        rows.append({"id": "billdoc_all", "title": "☑️ Select all"})
        rows.append({"id": "billdoc_clear", "title": "⬜ Clear"})
        rows.append({"id": "billdoc_inv",
                     "title": f"🧾 Invoice ({n} · {format_amount(sel_total)})"[:60]})
        rows.append({"id": "billdoc_rcpt",
                     "title": f"🧾 Receipt ({n} · {format_amount(sel_total)})"[:60]})
        rows.append({"id": "billdoc_back", "title": "⬅️ Back to customers"})

        return [list_response(
            header="🧾 Choose sales to bill",
            body=(f"*{cust_name}* — {n} selected · {format_amount(sel_total)}\n"
                  "Tap a sale to include/exclude, then tap Invoice or Receipt."),
            button_text="Select",
            sections=[{"title": "", "rows": rows}],
            no_paginate=True,
        )]

    @staticmethod
    def _label(tx, labeller):
        if labeller is not None:
            try:
                return labeller._clean_item_description(tx)
            except Exception:
                pass
        return (tx.get("item_name") or tx.get("description") or "Item").strip()

    def _generate(self, phone_number: str, cust_key: str, selected: set, kind: str) -> list:
        tx_ids = [t for t in selected if t]
        if not tx_ids:
            # Re-render so the user can pick at least one.
            return [text_response("⚠️ Select at least one sale first.")] + \
                self._render_selection(phone_number, cust_key, selected)

        self.session.reset(phone_number)
        marker = "__GEN_INVOICE__" if kind == "invoice" else "__GEN_RECEIPT__"
        # main.py resolves this marker → handle_multi_*_request(tx_ids) → ONE PDF.
        return [{"type": marker, "content": {"tx_ids": tx_ids}}]

    # ── numeric/text step (none needed; selection is all taps) ──
    def handle(self, phone_number: str, text: str, session: dict) -> list:
        # No free-text step in this flow; any text just re-shows the menu.
        return self.show_menu(phone_number)
