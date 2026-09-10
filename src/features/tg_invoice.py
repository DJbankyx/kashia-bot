"""tg_invoice — tap-first Telegram INVOICE BUILDER (Stage 4C).

Telegram ONLY. WhatsApp keeps the existing free-text invoice flow (invoices.py /
export._start_invoice) untouched. This module mirrors the tg_fastentry plumbing
(single in-place-edited message, session-stashed progress, own reserved callback
prefix) but assembles an INVOICE — a customer + line items + optional discount/
tax — and hands off to pdf_generator.generate_invoice(items=..., discount=...,
tax=...).

Design:
  * Own state states.INVOICE_BUILDER and own callback prefix tg_ui.TGINV_PREFIX
    ("__tginv__") so invoice taps never collide with the sale/purchase flow.
  * A single boxed card is edited in place as the invoice is built.
  * Line items can be added three ways: type "name qty price", pick from the
    catalog (price prefilled), or pull a past sale.
  * Discount/Tax hooks are present (wired fully in 4D); generate works without
    them.

Item shape (matches generate_invoice): {"description","quantity","amount","unit_cost"}.
"""

import logging
import re

from core import states
from utils import tg_ui
from utils.whatsapp_ui import format_amount

logger = logging.getLogger(__name__)

# Session context key holding invoice-builder progress.
INV = "__tginv"


class TGInvoice:
    """Tappable invoice builder for Telegram. Holds a router ref for engine access."""

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
    def catalog(self):
        return self.router.catalog

    @property
    def pdf(self):
        # PDFGenerator is wired on the export handler.
        return self.router.export.pdf_generator

    def _client(self):
        try:
            from main import get_bot
            return get_bot().get_client("telegram")
        except Exception as e:
            logger.warning(f"tg_invoice: no telegram client: {e}")
            return None

    def _bare(self, phone_number: str) -> str:
        from services.messaging_client import bare_recipient_id
        return bare_recipient_id(phone_number)

    # ── session helpers ──────────────────────────────────────────────────

    def _get(self, phone_number: str) -> dict:
        return (self.session.get(phone_number).get("context", {}) or {}).get(INV, {}) or {}

    def _save(self, phone_number: str, inv: dict):
        session = self.session.get(phone_number)
        context = dict(session.get("context", {}) or {})
        context[INV] = inv
        self.session.save(phone_number, states.INVOICE_BUILDER, context)

    def _render(self, phone_number: str, inv: dict, text: str, keyboard: list):
        """Send (first) or edit (subsequent) the single invoice-builder message."""
        client = self._client()
        if client is None:
            return
        to = self._bare(phone_number)
        mid = inv.get("msg_id")
        if mid:
            client.edit_message_text(to, mid, text, keyboard=keyboard)
        else:
            mid = client.send_and_get_id(to, text, keyboard=keyboard)
            inv["msg_id"] = mid
        self._save(phone_number, inv)

    def _edit_plain(self, phone_number: str, inv: dict, text: str):
        client = self._client()
        if client and inv.get("msg_id"):
            client.edit_message_text(self._bare(phone_number), inv["msg_id"], text, keyboard=[])

    # ── entry ────────────────────────────────────────────────────────────

    def start(self, phone_number: str) -> list:
        """Begin the invoice builder — ask who it's for. Returns [] (owns its
        own message)."""
        inv = {"step": "who", "customer": "", "items": [],
               "discount": None, "tax": None, "msg_id": None}
        self._save(phone_number, inv)
        recent = self._recent_contacts(phone_number)
        self._render(
            phone_number, inv,
            "🧾 *New Invoice*\n────────────────\n👤 Who is this invoice for?",
            tg_ui.inv_customer_keyboard(recent=recent),
        )
        return []

    # ── customer ─────────────────────────────────────────────────────────

    def _recent_contacts(self, phone_number: str) -> list:
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
            logger.warning(f"tg_invoice: contacts read failed: {e}")
            return []

    def _resolve_contact(self, phone_number: str, cid: str) -> str:
        for c_id, name in self._recent_contacts(phone_number):
            if str(c_id) == str(cid):
                return name
        return cid.replace("_", " ").title()

    # ── the main builder card ────────────────────────────────────────────

    def _show_builder(self, phone_number: str, inv: dict):
        items = inv.get("items", [])
        lines = ["🧾 *New Invoice*", "────────────────",
                 f"👤 *Customer:* {inv.get('customer') or '—'}", ""]
        if not items:
            lines.append("_No items yet. Add one below._")
        else:
            lines.append("*Items:*")
            for it in items:
                q = it.get("quantity", 1)
                qty_part = f" ×{q}" if q and int(q) != 1 else ""
                lines.append(f"  • {it['description']}{qty_part} = {format_amount(it['amount'])}")
            subtotal = self._subtotal(inv)
            lines.append("")
            lines.append(f"💰 *Subtotal: {format_amount(subtotal)}*")
            disc = inv.get("discount")
            tax = inv.get("tax")
            if disc:
                lines.append(f"  − Discount: {format_amount(disc.get('amount', 0))}")
            if tax:
                lines.append(f"  + Tax: {format_amount(tax.get('amount', 0))}")
            if disc or tax:
                lines.append(f"  *Total: {format_amount(self._total(inv))}*")
        inv["step"] = "builder"
        self._render(phone_number, inv, "\n".join(lines),
                     tg_ui.inv_builder_keyboard(has_items=bool(items)))

    def _subtotal(self, inv: dict) -> int:
        return sum(int(it.get("amount", 0)) for it in inv.get("items", []))

    def _total(self, inv: dict) -> int:
        total = self._subtotal(inv)
        disc = inv.get("discount")
        tax = inv.get("tax")
        if disc:
            total -= int(disc.get("amount", 0))
        if tax:
            total += int(tax.get("amount", 0))
        return max(0, total)

    # ── callback dispatch ────────────────────────────────────────────────

    def handle_callback(self, phone_number: str, action: str, value: str = "") -> list:
        inv = self._get(phone_number)

        if action == "cancel":
            self.session.reset(phone_number)
            self._edit_plain(phone_number, inv, "❌ Invoice cancelled.")
            return []

        if not inv:
            return []  # stale tap after reset

        if action == "cust":
            inv["customer"] = self._resolve_contact(phone_number, value)
            self._show_builder(phone_number, inv)
            return []

        if action == "custtype":
            inv["step"] = "await_customer_name"
            self._save(phone_number, inv)
            self._render(phone_number, inv,
                         "✍️ Type the customer's name:", [])
            return []

        if action == "add_type":
            inv["step"] = "await_item_text"
            self._save(phone_number, inv)
            self._render(phone_number, inv,
                         "➕ Type the item as:  *name qty price*\n"
                         "_e.g. Cement 20 4000  (20 bags at ₦4,000)_\n"
                         "_or just:  Delivery 10000_", [])
            return []

        if action == "add_cat":
            return self._show_catalog(phone_number, inv, page=0)

        if action == "catpage":
            return self._show_catalog(phone_number, inv, page=int(value or 0))

        if action == "cat":
            return self._add_catalog_item(phone_number, inv, value)

        if action == "add_sale":
            return self._show_sales(phone_number, inv)

        if action == "sale":
            return self._add_sale_item(phone_number, inv, value)

        if action == "rmlast":
            if inv.get("items"):
                inv["items"].pop()
            self._show_builder(phone_number, inv)
            return []

        if action == "back":
            self._show_builder(phone_number, inv)
            return []

        if action in ("discount", "tax"):
            # 4D wires the full discount/tax capture; for now prompt a simple amount.
            inv["step"] = f"await_{action}"
            self._save(phone_number, inv)
            label = "discount" if action == "discount" else "tax"
            self._render(phone_number, inv,
                         f"Enter the {label} amount (e.g. 5000), or type *0* to skip:", [])
            return []

        if action == "generate":
            return self._generate(phone_number, inv)

        return []

    # ── typed input ──────────────────────────────────────────────────────

    def handle_text(self, phone_number: str, text: str) -> list:
        inv = self._get(phone_number)
        if not inv:
            return []
        step = inv.get("step", "")
        t = (text or "").strip()

        if step == "await_customer_name":
            inv["customer"] = t.title() if t else "Customer"
            self._show_builder(phone_number, inv)
            return []

        if step == "await_item_text":
            item = self._parse_item_text(t)
            if not item:
                self._render(phone_number, inv,
                             "❌ Couldn't read that. Try:  *name qty price*  "
                             "(e.g. Cement 20 4000) or *name price* (e.g. Delivery 10000):", [])
                return []
            inv.setdefault("items", []).append(item)
            self._show_builder(phone_number, inv)
            return []

        if step in ("await_discount", "await_tax"):
            from utils.parser import parse_amount
            amt = parse_amount(t) or 0
            kind = "discount" if step == "await_discount" else "tax"
            inv[kind] = {"amount": int(amt)} if amt else None
            self._show_builder(phone_number, inv)
            return []

        return []

    def _parse_item_text(self, text: str):
        """Parse 'name qty price' or 'name price'. qty defaults to 1; amount =
        qty × price. Numbers may use K/M suffixes."""
        from utils.parser import parse_amount
        if not text:
            return None
        parts = text.split()
        # Find trailing numeric tokens (price, or qty + price).
        nums = []
        while parts and re.match(r'^[\d,]+[kKmM]?$', parts[-1]):
            nums.insert(0, parts.pop())
        if not parts or not nums:
            return None
        name = " ".join(parts).strip().title()
        if len(nums) >= 2:
            qty = int(re.sub(r'[^\d]', '', nums[0]) or "1")
            price = int(parse_amount(nums[1]) or 0)
        else:
            qty = 1
            price = int(parse_amount(nums[0]) or 0)
        if price <= 0:
            return None
        qty = max(1, qty)
        return {"description": name, "quantity": qty,
                "amount": qty * price, "unit_cost": price}

    # ── catalog line ─────────────────────────────────────────────────────

    def _show_catalog(self, phone_number: str, inv: dict, page: int) -> list:
        try:
            rows = self.catalog.get_product_list_for_recording(phone_number) or []
        except Exception:
            rows = []
        if not rows:
            self._render(phone_number, inv,
                         "🗂️ No catalog items yet. Use *Type an item* instead.",
                         tg_ui.inv_builder_keyboard(has_items=bool(inv.get("items"))))
            return []
        inv["step"] = "pick_cat"
        inv["cat_rows"] = rows
        self._save(phone_number, inv)
        self._render(phone_number, inv, "🗂️ Tap a product to add:",
                     tg_ui.inv_catalog_grid(rows, page=page))
        return []

    def _add_catalog_item(self, phone_number: str, inv: dict, row_id: str) -> list:
        # row id is "catrec_<key>"
        key = row_id[len("catrec_"):] if row_id.startswith("catrec_") else row_id
        price = 0
        name = key.replace("_", " ").title()
        try:
            prod = self.catalog.get_normalized_product(phone_number, key) or {}
            name = prod.get("name", name) or name
            price = int(prod.get("sale_price", 0) or 0) or int(prod.get("landing_cost", 0) or 0)
        except Exception:
            pass
        inv.setdefault("items", []).append(
            {"description": name, "quantity": 1, "amount": price, "unit_cost": price})
        self._show_builder(phone_number, inv)
        return []

    # ── past-sale line ───────────────────────────────────────────────────

    def _recent_sales(self, phone_number: str):
        try:
            txns = self.db.get_transactions(phone_number, limit=20) or []
        except Exception:
            return []
        out = []
        for t in txns:
            if t.get("type") not in ("sale", "income"):
                continue
            desc = self.pdf._clean_item_description(t) if hasattr(self.pdf, "_clean_item_description") \
                else (t.get("item_name") or t.get("description") or "Item")
            amt = int(t.get("amount", 0))
            out.append((t.get("transaction_id", ""), f"{desc} — {format_amount(amt)}"))
        return out[:8]

    def _show_sales(self, phone_number: str, inv: dict) -> list:
        sales = self._recent_sales(phone_number)
        if not sales:
            self._render(phone_number, inv,
                         "🧾 No past sales to pull from. Use *Type an item* instead.",
                         tg_ui.inv_builder_keyboard(has_items=bool(inv.get("items"))))
            return []
        inv["step"] = "pick_sale"
        self._save(phone_number, inv)
        self._render(phone_number, inv, "🧾 Tap a past sale to add as a line:",
                     tg_ui.inv_sales_keyboard(sales))
        return []

    def _add_sale_item(self, phone_number: str, inv: dict, tx_id: str) -> list:
        try:
            txns = self.db.get_transactions(phone_number, limit=50) or []
        except Exception:
            txns = []
        tx = next((t for t in txns if t.get("transaction_id") == tx_id), None)
        if tx:
            desc = self.pdf._clean_item_description(tx) if hasattr(self.pdf, "_clean_item_description") \
                else (tx.get("item_name") or tx.get("description") or "Item")
            amt = int(tx.get("amount", 0))
            qty = 1
            m = re.match(r'^(\d+)', str(tx.get("quantity", "1")))
            if m:
                qty = int(m.group(1))
            unit = amt // qty if qty else amt
            # Use this sale's customer as the invoice customer if none set yet.
            if not inv.get("customer"):
                v = tx.get("vendor", "")
                if v and v.lower() not in ("unknown", "walk-in", ""):
                    inv["customer"] = v
            inv.setdefault("items", []).append(
                {"description": desc, "quantity": qty, "amount": amt, "unit_cost": unit})
        self._show_builder(phone_number, inv)
        return []

    # ── generate + deliver ───────────────────────────────────────────────

    def _generate(self, phone_number: str, inv: dict) -> list:
        items = inv.get("items", [])
        if not items:
            self._show_builder(phone_number, inv)
            return []
        customer = inv.get("customer") or "Customer"
        total = self._total(inv)
        self._edit_plain(phone_number, inv, "🧾 Building your invoice…")
        self.session.reset(phone_number)
        # Hand off to the existing PDF engine (single source for document render).
        return self.pdf.handle_invoice_request(
            phone_number, customer, float(total), "",
            discount=inv.get("discount"), tax=inv.get("tax"),
            items=items,
        )
