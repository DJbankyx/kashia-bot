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
        due_services = []
        for svc in active:
            client = svc.get("client", "Unknown")
            service = svc.get("service", "Service")
            amount = int(svc.get("amount", 0))
            frequency = svc.get("frequency", "monthly")
            next_due = svc.get("next_due", "")
            rec_id = svc.get("id", "")

            if next_due and next_due <= now:
                status = "🔴 OVERDUE"
                overdue_count += 1
                due_services.append(svc)
            elif next_due and next_due <= (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"):
                status = "🟡 Due soon"
                due_services.append(svc)
            else:
                status = "🟢"

            lines.append(f"{status} *{client}*")
            lines.append(f"  💼 {service} — {format_amount(amount)}")
            lines.append(f"  🔄 {frequency.title()} | Next: {next_due or 'Not set'}")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")

        if overdue_count > 0:
            lines.append(f"⚠️ _{overdue_count} overdue — tap below to mark as done_")

        responses = [text_response("\n".join(lines))]

        # If there are due/overdue services, show them as tappable list
        if due_services:
            rows = []
            for svc in due_services[:8]:
                rec_id = svc.get("id", "")
                client = svc.get("client", "")
                service = svc.get("service", "")
                amount = int(svc.get("amount", 0))
                rows.append({
                    "id": f"rec_done_{rec_id}",
                    "title": f"✅ {client}"[:24],
                    "description": f"{service} — {format_amount(amount)}"[:72],
                })
            rows.append({
                "id": "rec_add",
                "title": "➕ Add New Recurring",
                "description": "Set up a new regular client",
            })
            responses.append(list_response(
                header="🔁 Actions",
                body="Mark jobs as done or add new:",
                button_text="Select",
                sections=[{"title": "Due / Actions", "rows": rows}]
            ))
        else:
            responses.append(button_response("Actions:", [
                {"id": "rec_add", "title": "➕ Add Recurring"},
                {"id": "menu_home", "title": "☰ Menu"},
            ]))

        return responses

    def handle_button(self, phone_number: str, button_id: str) -> list:
        """Handle recurring service buttons."""
        if button_id == "rec_add":
            return self._start_add(phone_number)

        if button_id.startswith("rec_freq_"):
            # Frequency selection from list — route to step handler
            session = self.session.get(phone_number)
            context = session.get("context", {})
            return self._step_frequency(phone_number, button_id, context)

        if button_id.startswith("rec_done_"):
            # Mark a recurring service as done
            rec_id = button_id[9:]
            return self._mark_done(phone_number, rec_id)

        if button_id.startswith("rec_delete_"):
            rec_id = button_id[11:]
            return self._delete_recurring(phone_number, rec_id)

        return self.show(phone_number)

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle text input during recurring service flows."""
        context = session.get("context", {})
        step = context.get("rec_step", "")
        text_s = text.strip()

        if text_s.lower() in ("cancel", "exit", "back", "done"):
            self.session.reset(phone_number)
            return [text_response("👍 Done.")]

        # Step-by-step guided add flow
        if step == "add_client":
            return self._step_client(phone_number, text_s, context)

        if step == "add_service":
            return self._step_service(phone_number, text_s, context)

        if step == "add_amount":
            return self._step_amount(phone_number, text_s, context)

        if step == "add_frequency":
            return self._step_frequency(phone_number, text_s, context)

        # Legacy: try to parse free-text input
        if step == "add_details":
            return self._parse_and_save(phone_number, text_s)

        return self._parse_and_save(phone_number, text_s)

    def _start_add(self, phone_number: str) -> list:
        """Start the add recurring service flow — step by step."""
        self.session.save(phone_number, states.RECURRING_SERVICES, {
            "rec_step": "add_client",
            "rec_data": {},
        })

        return [text_response(
            "➕ *Add Recurring Service*\n\n"
            "Step 1 of 4\n\n"
            "👤 *Who is the client?*\n\n"
            "_e.g. Mrs Ade, Alhaji Musa, Dangote Office_\n\n"
            "_Type *back* to cancel_"
        )]

    def _step_client(self, phone_number: str, text: str, context: dict) -> list:
        """Step 1: Save client name, ask for service."""
        if len(text) < 2:
            return [text_response("Please enter the client's name (at least 2 characters):")]

        rec_data = context.get("rec_data", {})
        rec_data["client"] = text.title()
        context["rec_data"] = rec_data
        context["rec_step"] = "add_service"
        self.session.save(phone_number, states.RECURRING_SERVICES, context)

        return [text_response(
            f"👤 Client: *{text.title()}*\n\n"
            f"Step 2 of 4\n\n"
            f"💼 *What service do you provide for them?*\n\n"
            f"_e.g. Office Cleaning, Hair Braiding, Delivery, Security_\n\n"
            f"_Type *back* to change client name_"
        )]

    def _step_service(self, phone_number: str, text: str, context: dict) -> list:
        """Step 2: Save service name, ask for amount."""
        if text.lower() == "back":
            context["rec_step"] = "add_client"
            self.session.save(phone_number, states.RECURRING_SERVICES, context)
            return [text_response(
                "👤 *Who is the client?*\n\n"
                "_e.g. Mrs Ade, Alhaji Musa, Dangote Office_"
            )]

        if len(text) < 2:
            return [text_response("Please enter the service name (at least 2 characters):")]

        rec_data = context.get("rec_data", {})
        rec_data["service"] = text.title()
        context["rec_data"] = rec_data
        context["rec_step"] = "add_amount"
        self.session.save(phone_number, states.RECURRING_SERVICES, context)

        return [text_response(
            f"💼 Service: *{text.title()}*\n\n"
            f"Step 3 of 4\n\n"
            f"💰 *How much do you charge for this?*\n\n"
            f"_e.g. 30000, 50K, 150K_\n\n"
            f"_Type *back* to change service_"
        )]

    def _step_amount(self, phone_number: str, text: str, context: dict) -> list:
        """Step 3: Save amount, ask for frequency."""
        if text.lower() == "back":
            context["rec_step"] = "add_service"
            self.session.save(phone_number, states.RECURRING_SERVICES, context)
            client = context.get("rec_data", {}).get("client", "")
            return [text_response(
                f"👤 Client: *{client}*\n\n"
                f"💼 *What service do you provide for them?*\n\n"
                f"_e.g. Office Cleaning, Hair Braiding, Delivery_"
            )]

        amount = parse_amount(text)
        if not amount:
            return [text_response("💰 Please enter a valid amount (e.g. 30000, 50K, 150K):")]

        rec_data = context.get("rec_data", {})
        rec_data["amount"] = int(amount)
        context["rec_data"] = rec_data
        context["rec_step"] = "add_frequency"
        self.session.save(phone_number, states.RECURRING_SERVICES, context)

        return [list_response(
            header="🔄 How often?",
            body=f"Step 4 of 4\n\n{rec_data.get('client', '')} — {rec_data.get('service', '')} — {format_amount(amount)}\n\nHow often do you do this job?",
            button_text="Select Frequency",
            sections=[{
                "title": "Frequency",
                "rows": [
                    {"id": "rec_freq_daily", "title": "📅 Daily", "description": "Every day"},
                    {"id": "rec_freq_weekly", "title": "📅 Weekly", "description": "Once a week"},
                    {"id": "rec_freq_biweekly", "title": "📅 Every 2 Weeks", "description": "Twice a month"},
                    {"id": "rec_freq_monthly", "title": "📅 Monthly", "description": "Once a month"},
                    {"id": "rec_freq_quarterly", "title": "📅 Quarterly", "description": "Every 3 months"},
                ]
            }]
        )]

    def _step_frequency(self, phone_number: str, text: str, context: dict) -> list:
        """Step 4: Save frequency (from button or text) and finalize."""
        # Handle button tap (rec_freq_weekly etc)
        freq_text = text.lower().replace("rec_freq_", "")

        freq_map = {
            "daily": "daily", "weekly": "weekly", "biweekly": "biweekly",
            "monthly": "monthly", "quarterly": "quarterly",
            "every 2 weeks": "biweekly", "every day": "daily",
            "once a week": "weekly", "once a month": "monthly",
        }
        frequency = freq_map.get(freq_text, "monthly")

        rec_data = context.get("rec_data", {})
        client = rec_data.get("client", "Client")
        service = rec_data.get("service", "Service")
        amount = rec_data.get("amount", 0)

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

    def _mark_done(self, phone_number: str, rec_id: str) -> list:
        """Mark a recurring service as done — records transaction and advances schedule."""
        user = self.db.get_user(phone_number) or {}
        recurring = user.get("recurring_services", [])

        # Find the service
        target = None
        for svc in recurring:
            if svc.get("id") == rec_id:
                target = svc
                break

        if not target:
            return [text_response("❓ Recurring service not found.")]

        client = target.get("client", "Client")
        service = target.get("service", "Service")
        amount = int(target.get("amount", 0))
        frequency = target.get("frequency", "monthly")

        # Record as a sale/income transaction
        self.db.save_transaction(
            phone_number,
            amount,
            "sale",
            f"{service} for {client}",
            "Service Revenue",
            vendor=client,
            sub_category="Recurring Service",
        )

        # Advance next_due
        days = FREQ_DAYS.get(frequency, 30)
        target["next_due"] = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        target["last_done"] = datetime.now().strftime("%Y-%m-%d")

        self.db.update_user_field(phone_number, "recurring_services", recurring)

        return [
            text_response(
                f"✅ *Job recorded!*\n\n"
                f"💼 {service} for {client}\n"
                f"💰 {format_amount(amount)}\n\n"
                f"📅 Next due: {target['next_due']}\n"
                f"_Schedule updated automatically._"
            ),
            button_response("What's next?", [
                {"id": "record_sale", "title": "💼 Record Job"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    def _delete_recurring(self, phone_number: str, rec_id: str) -> list:
        """Delete a recurring service."""
        user = self.db.get_user(phone_number) or {}
        recurring = user.get("recurring_services", [])

        name = ""
        new_list = []
        for svc in recurring:
            if svc.get("id") == rec_id:
                name = f"{svc.get('client', '')} — {svc.get('service', '')}"
            else:
                new_list.append(svc)

        self.db.update_user_field(phone_number, "recurring_services", new_list)

        return [
            text_response(f"🗑️ Removed: *{name}*" if name else "🗑️ Removed."),
            button_response("What's next?", [
                {"id": "rec_add", "title": "➕ Add Recurring"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

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
