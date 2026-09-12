# src/features/debt.py
"""Debt & Credit tracking — who owes whom, payments, reminders."""

import logging
import re
import traceback
from datetime import datetime

from core import states
from utils.parser import parse_amount
from utils.whatsapp_ui import text_response, button_response, list_response, format_amount

logger = logging.getLogger(__name__)


class DebtHandler:
    """Handles debt/credit tracking and payments."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database

    def _is_telegram(self, phone_number: str) -> bool:
        try:
            from services.messaging_client import platform_for_user
            return platform_for_user(phone_number) == "telegram"
        except Exception:
            return False

    @staticmethod
    def _age_days(item) -> int:
        """Days since the debt's due date (preferred) or last activity date.
        Returns 0 if no date on record or the date can't be parsed."""
        raw = (item.get("due_date") or item.get("last_date") or "").strip()
        if not raw:
            return 0
        # Take just the calendar date (drop any time portion), then parse.
        day = raw.replace("T", " ").split(" ")[0][:10]
        try:
            d = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return 0
        return max(0, (datetime.now() - d).days)

    @staticmethod
    def _bucket_label(days: int) -> str:
        if days >= 90:
            return "🔴 90+ days"
        if days >= 60:
            return "🟠 60+ days"
        if days >= 30:
            return "🟡 30+ days"
        return "🟢 Current"

    def show_summary(self, phone_number: str) -> list:
        """Show debt summary. Telegram gets the tap-first board with aging
        buckets + tappable people; WhatsApp keeps the classic text summary."""
        if self._is_telegram(phone_number):
            return self._debt_board(phone_number)

        debts = self.db.get_all_debtors(phone_number) or []
        i_owe = self.db.get_all_creditors(phone_number) or []

        total_owed_to_me = sum(float(d.get("amount", 0)) for d in debts if not d.get("paid"))
        total_i_owe = sum(float(d.get("amount", 0)) for d in i_owe if not d.get("paid"))

        lines = ["💳 *Debts & Credits*", ""]

        if total_owed_to_me > 0:
            lines.append(f"💰 *Owed to you:* {format_amount(total_owed_to_me)}")
            unpaid = [d for d in debts if not d.get("paid")][:5]
            for d in unpaid:
                name = d.get("name", "Unknown")
                amt = format_amount(d.get("amount", 0))
                lines.append(f"  • {name}: {amt}")
            if len([d for d in debts if not d.get("paid")]) > 5:
                lines.append(f"  _...and {len(unpaid) - 5} more_")
        else:
            lines.append("💰 No one owes you right now.")

        lines.append("")

        if total_i_owe > 0:
            lines.append(f"📝 *You owe:* {format_amount(total_i_owe)}")
            unpaid = [d for d in i_owe if not d.get("paid")][:5]
            for d in unpaid:
                name = d.get("name", "Unknown")
                amt = format_amount(d.get("amount", 0))
                lines.append(f"  • {name}: {amt}")
        else:
            lines.append("📝 You don't owe anyone.")

        return [button_response(
            "\n".join(lines),
            [
                {"id": "debt_record", "title": "➕ Record Debt"},
                {"id": "debt_payment", "title": "💵 Record Payment"},
                {"id": "debt_remind", "title": "⏰ Send Reminder"},
            ]
        )]

    # ────────────────────────── Telegram tap-first board ──────────────────────────

    def _aging_lines(self, items: list) -> list:
        """Summarise a set of debts by aging bucket → list of display lines.
        Only non-empty buckets are shown, in oldest-first order for urgency."""
        buckets = {"🔴 90+ days": 0.0, "🟠 60+ days": 0.0,
                   "🟡 30+ days": 0.0, "🟢 Current": 0.0}
        for it in items:
            amt = float(it.get("amount", 0) or 0)
            if amt <= 0:
                continue
            buckets[self._bucket_label(self._age_days(it))] += amt
        lines = []
        for label in ("🔴 90+ days", "🟠 60+ days", "🟡 30+ days", "🟢 Current"):
            if buckets[label] > 0:
                lines.append(f"   {label}: {format_amount(buckets[label])}")
        return lines

    @staticmethod
    def _is_expense_payee(item: dict) -> bool:
        """A creditor you owe from an EXPENSE (landlord, utilities…) rather than
        a goods supplier. Only the explicit expense_payee type counts; suppliers,
        'both', and legacy/unknown contacts stay on the supplier side (no faked
        split for data we can't classify)."""
        return (item.get("type") or "").lower().strip() == "expense_payee"

    def _debt_board(self, phone_number: str) -> list:
        """Tap-first Debt/Credit board (Telegram). Totals + aging buckets +
        a tappable row per person that opens their card."""
        debtors = [d for d in (self.db.get_all_debtors(phone_number) or [])
                   if float(d.get("amount", 0) or 0) > 0 and not d.get("paid")]
        creditors = [c for c in (self.db.get_all_creditors(phone_number) or [])
                     if float(c.get("amount", 0) or 0) > 0 and not c.get("paid")]

        total_in = sum(float(d.get("amount", 0) or 0) for d in debtors)
        total_out = sum(float(c.get("amount", 0) or 0) for c in creditors)
        net = total_in - total_out

        body = ["💳 *Debts & Credits*", ""]

        if total_in > 0:
            body.append(f"💰 *Owed to you:* {format_amount(total_in)}")
            body.extend(self._aging_lines(debtors))
        else:
            body.append("💰 *Owed to you:* nothing outstanding")
        body.append("")

        # Split payables for CRM clarity: real suppliers (goods) vs expense
        # payees (rent, utilities, etc). Legacy/unknown → supplier side.
        supplier_creditors = [c for c in creditors if not self._is_expense_payee(c)]
        expense_creditors = [c for c in creditors if self._is_expense_payee(c)]
        owed_suppliers = sum(float(c.get("amount", 0) or 0) for c in supplier_creditors)
        owed_expenses = sum(float(c.get("amount", 0) or 0) for c in expense_creditors)

        if total_out > 0:
            body.append(f"📝 *You owe:* {format_amount(total_out)}")
            # Only show the split when it actually splits (both sides present),
            # otherwise the single-line total is cleaner.
            if owed_suppliers > 0 and owed_expenses > 0:
                body.append(f"   🏭 Suppliers: {format_amount(owed_suppliers)}")
                body.append(f"   🧾 Expenses: {format_amount(owed_expenses)}")
            body.extend(self._aging_lines(creditors))
        else:
            body.append("📝 *You owe:* nothing outstanding")

        if total_in > 0 or total_out > 0:
            body.append("")
            net_label = "net in your favour" if net >= 0 else "net you owe"
            body.append(f"⚖️ *Balance:* {format_amount(abs(net))} _{net_label}_")

        # Tappable people. Sort each side oldest-first so the most urgent
        # debts surface at the top of the list.
        rows = []
        if debtors:
            rows.append({"id": "debt_noop", "title": "─── 💰 Owed to you ───"})
            for d in sorted(debtors, key=lambda x: -self._age_days(x))[:12]:
                name = d.get("name", "Unknown")
                age = self._age_days(d)
                flag = self._bucket_label(age).split(" ")[0]  # colour dot only
                rows.append({
                    "id": f"debt_person_in_{name}"[:60],
                    "title": f"{flag} {name} · {format_amount(d.get('amount', 0))}"[:60],
                })
        def _creditor_rows(items):
            for c in sorted(items, key=lambda x: -self._age_days(x))[:12]:
                name = c.get("name", "Unknown")
                age = self._age_days(c)
                flag = self._bucket_label(age).split(" ")[0]
                rows.append({
                    "id": f"debt_person_out_{name}"[:60],
                    "title": f"{flag} {name} · {format_amount(c.get('amount', 0))}"[:60],
                })

        if creditors:
            if supplier_creditors and expense_creditors:
                # Both kinds present → show two labelled groups.
                rows.append({"id": "debt_noop", "title": "─── 📝 You owe · 🏭 Suppliers ───"})
                _creditor_rows(supplier_creditors)
                rows.append({"id": "debt_noop", "title": "─── 📝 You owe · 🧾 Expenses ───"})
                _creditor_rows(expense_creditors)
            else:
                rows.append({"id": "debt_noop", "title": "─── 📝 You owe ───"})
                _creditor_rows(creditors)

        # Always-available actions.
        rows.append({"id": "debt_record", "title": "➕ Record a new debt"})

        if not debtors and not creditors:
            return [button_response(
                "\n".join(body) + "\n\n_All clear — no open debts either way._",
                [{"id": "debt_record", "title": "➕ Record Debt"}]
            )]

        return [list_response(
            header="💳 Debts & Credits",
            body="\n".join(body),
            button_text="Open",
            sections=[{"title": "", "rows": rows}],
            no_paginate=True,
        )]

    def _person_card(self, phone_number: str, name: str, direction: str) -> list:
        """A single person's debt card with tap actions. direction:
        'in' = they owe me, 'out' = I owe them."""
        source = (self.db.get_all_debtors(phone_number) if direction == "in"
                  else self.db.get_all_creditors(phone_number)) or []
        match = next((x for x in source
                      if x.get("name", "").lower() == name.lower()
                      and float(x.get("amount", 0) or 0) > 0), None)
        if not match:
            return [text_response(
                f"✅ Nothing outstanding with *{name}* — that debt looks settled."
            )] + self._debt_board(phone_number)

        amount = float(match.get("amount", 0) or 0)
        age = self._age_days(match)
        bucket = self._bucket_label(age)
        contact_id = match.get("contact_id", "")
        reason = match.get("reason") or match.get("note") or ""

        if direction == "in":
            headline = f"💰 *{name}* owes you {format_amount(amount)}"
            pay_id = f"debt_payin_{name}"[:60]
            pay_title = "💵 Record a payment"
        else:
            headline = f"📝 You owe *{name}* {format_amount(amount)}"
            pay_id = f"debt_payout_{name}"[:60]
            pay_title = "💵 Record a payment"

        body = [headline, f"⏱ Age: {bucket} ({age} days)"]
        if reason:
            body.append(f"📝 {reason}")

        rows = [{"id": pay_id, "title": pay_title}]
        # Settle-in-full pre-fills the exact outstanding amount.
        rows.append({
            "id": f"debt_settle_{direction}_{name}"[:60],
            "title": f"✅ Settle in full ({format_amount(amount)})"[:60],
        })
        if direction == "in" and contact_id:
            rows.append({"id": f"debt_remind_{contact_id}"[:60],
                         "title": "⏰ Send a reminder"})
        rows.append({"id": "debt_back_board", "title": "⬅️ Back to board"})

        return [list_response(
            header="💳 Debt details",
            body="\n".join(body),
            button_text="Action",
            sections=[{"title": "", "rows": rows}],
            no_paginate=True,
        )]

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle debt-related states."""
        state = session.get("state", "")
        context = session.get("context", {})
        text_lower = text.lower().strip()

        if state == states.DEBT_RECORDING:
            return self._handle_recording(phone_number, text, context)

        if state == states.DEBT_CONFIRMING:
            return self._handle_confirming(phone_number, text, context)

        if state == states.DEBT_PAYMENT:
            return self._handle_payment(phone_number, text, context)

        return self.show_summary(phone_number)

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle debt-related buttons."""
        if button_id == "debt_record":
            self.session.save(phone_number, states.DEBT_RECORDING, {
                "debt_step": "ask_direction",
            })
            return [button_response(
                "💳 Who owes whom?",
                [
                    {"id": "debt_they_owe", "title": "They owe me"},
                    {"id": "debt_i_owe", "title": "I owe them"},
                    {"id": "btn_cancel", "title": "❌ Cancel"},
                ]
            )]

        if button_id in ("debt_they_owe", "debt_i_owe"):
            direction = "they_owe" if button_id == "debt_they_owe" else "i_owe"
            self.session.save(phone_number, states.DEBT_RECORDING, {
                "debt_step": "ask_name",
                "direction": direction,
            })
            return [text_response("👤 Who? (Type their name)")]

        if button_id == "debt_payment":
            return self._start_payment_flow(phone_number)

        # ── Telegram board: divider rows are inert ──
        if button_id == "debt_noop":
            return []
        if button_id == "debt_back_board":
            return self._debt_board(phone_number)

        # ── Telegram board: open a person's card ──
        if button_id.startswith("debt_person_in_"):
            return self._person_card(phone_number, button_id[15:], "in")
        if button_id.startswith("debt_person_out_"):
            return self._person_card(phone_number, button_id[16:], "out")

        # ── Telegram board: settle a debt in full (pre-fill the full amount) ──
        if button_id.startswith("debt_settle_in_"):
            return self._settle_in_full(phone_number, button_id[15:], "in")
        if button_id.startswith("debt_settle_out_"):
            return self._settle_in_full(phone_number, button_id[16:], "out")

        # Person + direction chosen for a payment.
        if button_id.startswith("debt_payin_"):
            return self._start_payment_amount(phone_number, button_id[11:], "in")
        if button_id.startswith("debt_payout_"):
            return self._start_payment_amount(phone_number, button_id[12:], "out")

        if button_id == "debt_remind":
            return self._show_remind_list(phone_number)

        # ── Send reminder to specific debtor (debt_remind_[contact_id]) ──
        if button_id.startswith("debt_remind_"):
            contact_id = button_id[12:]  # after "debt_remind_"
            return self._send_reminder(phone_number, contact_id)

        return self.show_summary(phone_number)

    def _handle_recording(self, phone_number: str, text: str, context: dict) -> list:
        """Step through debt recording."""
        step = context.get("debt_step", "ask_direction")

        if step == "ask_name":
            context["name"] = text.strip()
            context["debt_step"] = "ask_amount"
            self.session.save(phone_number, states.DEBT_RECORDING, context)
            return [text_response(f"💰 How much does *{text.strip()}* {'owe you' if context.get('direction') == 'they_owe' else 'you owe them'}?")]

        if step == "ask_amount":
            amount = parse_amount(text)
            if not amount:
                return [text_response("Please enter a valid amount (e.g. 50000, 150K):")]

            context["amount"] = float(amount)
            context["debt_step"] = "ask_reason"
            self.session.save(phone_number, states.DEBT_RECORDING, context)
            return [text_response("📝 What was it for? (or type *skip*)")]

        if step == "ask_reason":
            reason = text.strip() if text.lower().strip() != "skip" else ""
            context["reason"] = reason
            name = context.get("name", "")
            amount = context.get("amount", 0)
            direction = context.get("direction", "they_owe")

            self.session.save(phone_number, states.DEBT_CONFIRMING, context)

            dir_label = f"*{name}* owes you" if direction == "they_owe" else f"You owe *{name}*"
            reason_line = f"\n📝 Reason: {reason}" if reason else ""

            return [button_response(
                f"💳 Confirm:\n\n{dir_label} {format_amount(amount)}{reason_line}",
                [
                    {"id": "btn_yes", "title": "✅ Confirm"},
                    {"id": "btn_no", "title": "❌ Cancel"},
                ]
            )]

        return self.show_summary(phone_number)

    def _handle_confirming(self, phone_number: str, text: str, context: dict) -> list:
        """Confirm and save debt, then ask if they want to also record as transaction."""
        if text.lower() in ("yes", "y", "confirm", "btn_yes", "✅ confirm"):
            try:
                name = context.get("name", "")
                amount = context.get("amount", 0)
                direction = context.get("direction", "they_owe")
                reason = context.get("reason", "")

                if direction == "they_owe":
                    self.db.record_debt(phone_number, name, float(amount), 'owed_to_me', reason)
                    # Also record as a credit sale transaction
                    desc = reason or f"Credit sale to {name}"
                    self.db.save_transaction(
                        phone_number, int(amount), "sale", desc,
                        "Sales & Income", vendor=name,
                        payment_method="credit",
                    )
                    self.session.reset(phone_number)
                    return [
                        text_response(
                            f"✅ Recorded! *{name}* owes you {format_amount(amount)}.\n"
                            f"💰 Also saved as a credit sale."
                        ),
                        button_response("What's next?", [
                            {"id": "record_sale", "title": "💰 Record Sale"},
                            {"id": "menu_debts", "title": "💳 View Debts"},
                            {"id": "menu_home", "title": "☰ Menu"},
                        ])
                    ]
                else:
                    self.db.record_debt(phone_number, name, float(amount), 'i_owe', reason)
                    # Also record as a credit purchase transaction
                    desc = reason or f"Credit purchase from {name}"
                    self.db.save_transaction(
                        phone_number, int(amount), "purchase", desc,
                        "Goods & Stock", vendor=name,
                        payment_method="credit",
                    )
                    self.session.reset(phone_number)
                    return [
                        text_response(
                            f"✅ Recorded! You owe *{name}* {format_amount(amount)}.\n"
                            f"📦 Also saved as a credit purchase."
                        ),
                        button_response("What's next?", [
                            {"id": "record_purchase", "title": "📦 Record Purchase"},
                            {"id": "menu_debts", "title": "💳 View Debts"},
                            {"id": "menu_home", "title": "☰ Menu"},
                        ])
                    ]
            except Exception as e:
                logger.error(f"Debt save error: {e}\n{traceback.format_exc()}")
                self.session.reset(phone_number)
                return [text_response(f"❌ Error saving debt. Please try again.")]

        self.session.reset(phone_number)
        return [text_response("👍 Cancelled.")]

    def _handle_payment(self, phone_number: str, text: str, context: dict) -> list:
        """Handle payment recording from text (e.g. 'Dangote paid 10000')."""
        # ── New guided path: person + direction already chosen, ask amount ──
        if context.get("debt_step") == "ask_pay_amount":
            return self._apply_directed_payment(phone_number, text, context)

        payment_text = context.get("payment_text", text)
        text_lower = payment_text.lower()

        # Try to extract name and amount
        # Pattern: "[name] paid [amount]" or "received [amount] from [name]"
        amount = parse_amount(payment_text)
        if not amount:
            self.session.reset(phone_number)
            return [text_response("💰 How much was paid? (Please include an amount)")]

        # Extract name — everything before "paid/settled/cleared"
        name = ""
        for verb in ["paid", "settled", "cleared"]:
            if verb in text_lower:
                parts = text_lower.split(verb)
                if parts[0].strip():
                    name = payment_text[:len(parts[0])].strip()
                break

        if not name:
            # Try "received from [name]"
            match = re.search(r'from\s+(.+?)(?:\s+\d|\s*$)', payment_text, re.IGNORECASE)
            if match:
                name = match.group(1).strip()

        if name:
            # Record payment — settle debt AND record as income transaction
            try:
                self.db.settle_debt(phone_number, name, float(amount), 'owed_to_me')
                # Also record as income transaction (debt payment received)
                self.db.save_transaction(
                    phone_number, int(amount), "sale",
                    f"Debt payment from {name}",
                    "Sales & Income", vendor=name,
                    payment_method="cash",
                )
                self.session.reset(phone_number)
                return [
                    text_response(
                        f"✅ Payment recorded! *{name}* paid {format_amount(amount)}.\n"
                        f"💰 Debt reduced and payment logged."
                    ),
                    button_response("What's next?", [
                        {"id": "menu_debts", "title": "💳 View Debts"},
                        {"id": "record_sale", "title": "💰 Record Sale"},
                        {"id": "menu_home", "title": "☰ Menu"},
                    ])
                ]
            except Exception as e:
                logger.error(f"Payment record error: {e}")
                self.session.reset(phone_number)
                return [text_response(f"✅ Got it — {format_amount(amount)} payment from *{name}*.")]
        else:
            self.session.reset(phone_number)
            return [text_response(f"✅ {format_amount(amount)} payment noted.")]

    def _start_payment_flow(self, phone_number: str) -> list:
        """Show BOTH directions for recording a payment:
          - money owed TO you (a customer repaid you) → collect,
          - money YOU owe (you repaid a supplier)      → repay.
        Previously only the first was offered, so if you only OWED money the
        flow wrongly said 'no outstanding debts'."""
        debtors = [d for d in (self.db.get_all_debtors(phone_number) or [])
                   if d.get("amount", 0) > 0]
        creditors = [c for c in (self.db.get_all_creditors(phone_number) or [])
                     if c.get("amount", 0) > 0]

        if not debtors and not creditors:
            return [text_response(
                "✅ No outstanding debts either way — nothing to record a payment against."
            )]

        rows = []
        # Owed to me → tapping records a payment received (in:<name>).
        for d in debtors[:8]:
            name = d.get("name", "Unknown")
            rows.append({"id": f"debt_payin_{name}"[:60],
                         "title": f"⬅️ {name} pays me · {format_amount(d.get('amount', 0))}"[:60]})
        # I owe them → tapping records a repayment I made (out:<name>).
        for c in creditors[:8]:
            name = c.get("name", "Unknown")
            rows.append({"id": f"debt_payout_{name}"[:60],
                         "title": f"➡️ I pay {name} · {format_amount(c.get('amount', 0))}"[:60]})

        return [list_response(
            header="💵 Record Payment",
            body="Who paid, and which way?\n"
                 "_⬅️ = someone repaid you · ➡️ = you repaid a supplier_",
            button_text="Select",
            sections=[{"title": "", "rows": rows}]
        )]

    def _start_payment_amount(self, phone_number: str, name: str, direction: str) -> list:
        """After picking a person + direction, ask how much was paid."""
        self.session.save(phone_number, states.DEBT_PAYMENT, {
            "debt_step": "ask_pay_amount",
            "pay_name": name,
            "pay_direction": direction,   # 'in' (they paid me) | 'out' (I paid them)
        })
        who = f"*{name}* paid you" if direction == "in" else f"you paid *{name}*"
        return [text_response(
            f"💵 How much did {who}?\n\n_e.g. 50000, 150K. Type the amount._"
        )]

    def _settle_in_full(self, phone_number: str, name: str, direction: str) -> list:
        """Settle a debt in full: look up the exact outstanding amount and run
        the directed payment for that figure (reuses _apply_directed_payment)."""
        source = (self.db.get_all_debtors(phone_number) if direction == "in"
                  else self.db.get_all_creditors(phone_number)) or []
        match = next((x for x in source
                      if x.get("name", "").lower() == name.lower()
                      and float(x.get("amount", 0) or 0) > 0), None)
        if not match:
            return [text_response(
                f"✅ Nothing outstanding with *{name}* — already settled."
            )] + self._debt_board(phone_number)
        amount = float(match.get("amount", 0) or 0)
        context = {"pay_name": name, "pay_direction": direction}
        return self._apply_directed_payment(phone_number, str(int(amount)), context)

    def _apply_directed_payment(self, phone_number: str, text: str, context: dict) -> list:
        """Settle the chosen debt in the chosen direction + record the matching
        transaction. direction 'in' = they repaid me; 'out' = I repaid them."""
        name = context.get("pay_name", "")
        direction = context.get("pay_direction", "in")
        amount = parse_amount(text)
        if not amount:
            return [text_response("💵 Please type a valid amount (e.g. 50000, 150K).")]
        amount = float(amount)
        try:
            if direction == "in":
                # A customer repaid a debt they owed me → reduce owed_to_me, log income.
                remaining = self.db.settle_debt(phone_number, name, amount, 'owed_to_me')
                self.db.save_transaction(
                    phone_number, int(amount), "sale",
                    f"Debt repayment from {name}", "Sales & Income",
                    vendor=name, payment_method="cash")
                headline = f"✅ *{name}* repaid you {format_amount(amount)}."
            else:
                # I repaid a supplier I owed → reduce i_owe, log the outgoing payment.
                remaining = self.db.settle_debt(phone_number, name, amount, 'i_owe')
                self.db.save_transaction(
                    phone_number, int(amount), "expense",
                    f"Debt repayment to {name}", "Debt Repayment",
                    vendor=name, payment_method="cash")
                headline = f"✅ You repaid *{name}* {format_amount(amount)}."
        except Exception as e:
            logger.error(f"Directed payment error: {e}\n{traceback.format_exc()}")
            self.session.reset(phone_number)
            return [text_response("❌ Couldn't record that payment. Please try again.")]

        self.session.reset(phone_number)
        rem = int(remaining or 0)
        bal_line = (f"\n📝 Remaining balance: *{format_amount(rem)}*"
                    if rem > 0 else "\n✅ Fully settled — no balance left.")
        return [
            text_response(headline + bal_line),
            button_response("What's next?", [
                {"id": "menu_debts", "title": "💳 View Debts"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    def _show_remind_list(self, phone_number: str) -> list:
        """Show debtors as tappable list for sending reminders."""
        debts = self.db.get_all_debtors(phone_number) or []
        unpaid = [d for d in debts if not d.get("paid") and d.get("amount", 0) > 0]

        if not unpaid:
            return [text_response("✅ No outstanding debts to remind about.")]

        rows = []
        for d in unpaid[:10]:
            name       = d.get("name", "Unknown")
            amount     = d.get("amount", 0)
            contact_id = d.get("contact_id", name.lower().replace(" ", "_"))
            rows.append({
                "id": f"debt_remind_{contact_id}",
                "title": f"⏰ {name}"[:24],
                "description": f"Owes {format_amount(amount)}"[:72],
            })

        return [list_response(
            header="⏰ Send Reminder",
            body="Pick a debtor to send a payment reminder:",
            button_text="Select Person",
            sections=[{"title": "Debtors", "rows": rows}]
        )]

    def _send_reminder(self, phone_number: str, contact_id: str) -> list:
        """Send a payment reminder to a debtor via WhatsApp (or provide copy text)."""
        # Look up contact
        contact_name = contact_id.replace("_", " ").title()
        contact = self.db.get_contact_by_name(phone_number, contact_name)

        if not contact:
            return [text_response(f"❓ Contact *{contact_name}* not found.")]

        name         = contact.get("name", contact_name)
        debt_amount  = int(contact.get("debt_owed_to_me", 0))
        debtor_phone = contact.get("contact_phone", "")

        if debt_amount <= 0:
            return [text_response(f"✅ *{name}* doesn't owe you anything!")]

        # Get business name for the reminder message
        user = self.db.get_user(phone_number)
        business_name = user.get("business_name", "your supplier") if user else "your supplier"

        # Build the reminder message
        reminder_text = (
            f"Hello {name},\n\n"
            f"This is a friendly reminder from *{business_name}* "
            f"that you have an outstanding balance of *₦{debt_amount:,}*.\n\n"
            f"Please make payment at your earliest convenience.\n\n"
            f"Thank you! 🙏"
        )

        if debtor_phone and len(debtor_phone) >= 10:
            # Has phone number — return marker for main.py to send via WhatsApp
            return [
                {"type": "__SEND_REMINDER__", "content": {
                    "debtor_phone": debtor_phone,
                    "debtor_name": name,
                    "reminder_text": reminder_text,
                    "amount": debt_amount,
                }},
                text_response(
                    f"✅ *Reminder sent to {name}!*\n\n"
                    f"📱 Sent to: {debtor_phone}\n"
                    f"💰 Amount: ₦{debt_amount:,}\n\n"
                    f"_They'll receive the message on WhatsApp._"
                )
            ]
        else:
            # No phone number — show copy-paste text
            return [text_response(
                f"⏰ *Reminder for {name}*\n\n"
                f"📱 No phone number saved for {name}.\n\n"
                f"Copy and send this message manually:\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{reminder_text}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"_To save their number: type \"save number {name} 08012345678\"_"
            )]
