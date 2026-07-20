# src/features/recurring.py
"""Recurring Services — track regular client jobs with reminders.

Data stored on user profile as:
  recurring_services: [
    {
      "id": "rec_001",
      "client": "Mrs Oguntuase",
      "service": "Office Cleaning",
      "amount": 50000,
      "frequency": "monthly",  # weekly, biweekly, monthly
      "next_due": "2026-08-01",
      "last_done": "2026-07-01",
      "active": True
    }
  ]
"""

import logging
import re
from datetime import datetime, timedelta

from core import states
from utils.whatsapp_ui import text_response, button_response, list_response, format_amount
from utils.parser import parse_amount

logger = logging.getLogger(__name__)

# Frequency → days mapping
FREQ_DAYS = {
    "daily": 1,
    "weekly": 7,
    "biweekly": 14,
    "monthly": 30,
    "quarterly": 90,
}


class RecurringHandler:
    """Manage recurring services for service providers."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database

    def show(self, phone_number: str) -> list:
        """Show all recurring services."""
        user = self.db.get_user(phone_number) or {}
        recurring = user.get("recurring_services", [])
        active = [r for r in recurring if r.get("active", True)]

        if not active:
            return [
                text_response(
                    "🔁 *Recurring Services*\n\n"
                    "No recurring services set up yet.\n\n"
                    "Add regular client jobs that repeat on a schedule.\n\n"
                    "Type:\n"
                    "_[client] [service] [amount] [frequency]_\n\n"
                    "Example:\n"
                    "_Mrs Ade cleaning 30K weekly_\n"
                    "_Dangote delivery 100K monthly_"
                ),
                button_response("Or:", [
                    {"id": "rec_add", "title": "➕ Add Recurring"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ])
            ]

        now = datetime.now().strftime("%Y-%m-%d")
        lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            "🔁  *Recurring Services*",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        overdue_count = 0
        for svc in active:
            client = svc.get("client", "Unknown")
            service = svc.get("service", "Service")
            amount = int(svc.get("amount", 0))
            frequency = svc.get("frequency", "monthly")
            next_due = svc.get("next_due", "")

            if next_due and next_due <= now:
                status = "🔴 OVERDUE"
                overdue_count += 1
            elif next_due and next_due <= (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"):
                status = "🟡 Due soon"
            else:
                status = "🟢"

            lines.append(f"{status} *{client}*")
            lines.append(f"  💼 {service} — {format_amount(amount)}")
            lines.append(f"  🔄 {frequency.title()} | Next: {next_due or 'Not set'}")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")

        if overdue_count > 0:
            lines.append(f"⚠️ _{overdue_count} overdue — record them to update schedule_")

        return [
            text_response("\n".join(lines)),
            button_response("Actions:", [
                {"id": "rec_add", "title": "➕ Add Recurring"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    def handle_button(self, phone_number: str, button_id: str) -> list:
        """Handle recurring service buttons."""
        if button_id == "rec_add":
            return self._start_add(phone_number)

        return self.show(phone_number)

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle text input during recurring service flows."""
        context = session.get("context", {})
        step = context.get("rec_step", "")
        text_s = text.strip()

        if text_s.lower() in ("cancel", "exit", "back", "done"):
            self.session.reset(phone_number)
            return [text_response("👍 Done.")]

        if step == "add_details":
            return self._parse_and_save(phone_number, text_s)

        # Try to parse free-text input
        return self._parse_and_save(phone_number, text_s)

    def _start_add(self, phone_number: str) -> list:
        """Start the add recurring service flow."""
        self.session.save(phone_number, states.RECURRING_SERVICES, {
            "rec_step": "add_details",
        })

        return [text_response(
            "➕ *Add Recurring Service*\n\n"
            "Type all details in one line:\n"
            "_[client] [service] [amount] [frequency]_\n\n"
            "Examples:\n"
            "• _Mrs Ade cleaning 30K weekly_\n"
            "• _Alhaji office cleaning 50K monthly_\n"
            "• _Chief Obi security 200K monthly_\n"
            "• _Sandra braiding 15K biweekly_\n\n"
            "Frequencies: daily, weekly, biweekly, monthly, quarterly\n\n"
            "_Type *cancel* to go back._"
        )]

    def _parse_and_save(self, phone_number: str, text: str) -> list:
        """Parse '[client] [service] [amount] [frequency]' and save."""
        # Extract amount
        amount = parse_amount(text)
        if not amount:
            return [text_response(
                "💰 Please include an amount.\n\n"
                "Example: _Mrs Ade cleaning 30K weekly_"
            )]

        # Extract frequency
        text_lower = text.lower()
        frequency = "monthly"  # default
        for freq in FREQ_DAYS:
            if freq in text_lower:
                frequency = freq
                break

        # Remove amount and frequency from text to get client + service
        # Remove frequency word
        remaining = re.sub(r'\b(daily|weekly|biweekly|monthly|quarterly)\b', '', text, flags=re.IGNORECASE)
        # Remove amount patterns
        remaining = re.sub(r'[\u20a6#N]?\d[\d,]*[kKmM]?', '', remaining)
        remaining = remaining.strip().strip(',').strip()

        # Split remaining into client and service (heuristic: first word(s) = client)
        words = remaining.split()
        if len(words) >= 2:
            # Try to find a service keyword
            service_words = {'cleaning', 'delivery', 'repair', 'braiding', 'maintenance',
                           'security', 'laundry', 'fumigation', 'gardening', 'cooking',
                           'driving', 'tutoring', 'consulting', 'painting', 'plumbing',
                           'haircut', 'barbing', 'makeup', 'nails', 'massage', 'tailoring'}

            client_parts = []
            service_parts = []
            found_service = False
            for word in words:
                if word.lower() in service_words or found_service:
                    service_parts.append(word)
                    found_service = True
                else:
                    client_parts.append(word)

            client = " ".join(client_parts).strip().title() if client_parts else "Client"
            service = " ".join(service_parts).strip().title() if service_parts else "Service"
        elif len(words) == 1:
            client = words[0].title()
            service = "Service"
        else:
            client = "Client"
            service = "Service"

        # Calculate next due date
        days = FREQ_DAYS.get(frequency, 30)
        next_due = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

        # Save to user profile
        import time
        rec_id = f"rec_{int(time.time()) % 100000:05d}"

        user = self.db.get_user(phone_number) or {}
        recurring = user.get("recurring_services", [])
        recurring.append({
            "id": rec_id,
            "client": client,
            "service": service,
            "amount": int(amount),
            "frequency": frequency,
            "next_due": next_due,
            "last_done": datetime.now().strftime("%Y-%m-%d"),
            "active": True,
        })
        self.db.update_user_field(phone_number, "recurring_services", recurring)

        self.session.reset(phone_number)

        return [
            text_response(
                f"✅ *Recurring service added!*\n\n"
                f"👤 Client: *{client}*\n"
                f"💼 Service: {service}\n"
                f"💰 Amount: {format_amount(amount)}\n"
                f"🔄 Frequency: {frequency.title()}\n"
                f"📅 Next due: {next_due}\n\n"
                f"_I'll remind you when it's due._"
            ),
            button_response("What's next?", [
                {"id": "rec_add", "title": "➕ Add Another"},
                {"id": "record_sale", "title": "💼 Record Job"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    def check_due_reminders(self, phone_number: str) -> list:
        """Check if any recurring services are due. Called on greeting/session start."""
        user = self.db.get_user(phone_number) or {}
        recurring = user.get("recurring_services", [])
        now = datetime.now().strftime("%Y-%m-%d")

        due = [r for r in recurring if r.get("active", True)
               and r.get("next_due", "") <= now]

        if not due:
            return []

        lines = ["🔔 *Recurring services due:*", ""]
        for svc in due[:5]:
            client = svc.get("client", "")
            service = svc.get("service", "")
            amount = int(svc.get("amount", 0))
            lines.append(f"  • {client}: {service} — {format_amount(amount)}")

        lines.append("")
        lines.append("_Record these jobs to update their schedule._")

        return [text_response("\n".join(lines))]
