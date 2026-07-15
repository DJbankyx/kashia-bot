# src/features/personal_info.py
"""Personal Information — profile forms for bank details, address, name, PIN."""

import hashlib
import logging
import re

from core import states
from utils.whatsapp_ui import (
    text_response, button_response, list_response, format_amount
)

logger = logging.getLogger(__name__)

# State name for personal info multi-step flows
PERSONAL_INFO_STATE = "PERSONAL_INFO"


class PersonalInfoHandler:
    """
    Handles all Personal Information section flows.

    Covers:
      pi_business_name  — view/edit business name
      pi_phone          — view registered phone
      pi_bank           — add/edit bank details (3-step)
      pi_address        — add/edit business address
      pi_email          — add/edit email
      pi_edit           — show edit menu (same as the sub-menu)
      set_password      — set/change 4-digit PIN
    """

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database

    # ─────────────────────────────────────────────────────────
    # BUTTON ENTRY POINTS
    # ─────────────────────────────────────────────────────────

    def handle_button(self, phone_number: str, button_id: str) -> list:
        """Route pi_* and set_password buttons."""

        if button_id == "pi_business_name":
            return self._show_field(phone_number, "business_name")

        if button_id == "pi_phone":
            return self._show_phone(phone_number)

        if button_id == "pi_bank":
            return self._start_bank_details(phone_number)

        if button_id == "pi_address":
            return self._start_edit_field(
                phone_number, "business_address",
                "📍 *Business Address*",
                "Type your shop/market address:\n\n"
                "_e.g. Shop 12, Balogun Market, Lagos Island_"
            )

        if button_id == "pi_email":
            return self._start_edit_field(
                phone_number, "email",
                "📧 *Email Address*",
                "Type your email address:\n\n"
                "_Used for digital receipts (optional)_"
            )

        if button_id == "pi_edit":
            return self._show_edit_menu(phone_number)

        if button_id == "set_password":
            return self._start_set_pin(phone_number)

        # Confirm/cancel buttons from within flows
        if button_id == "pi_confirm_save":
            return self._confirm_save(phone_number)

        if button_id == "pi_cancel":
            self.session.reset(phone_number)
            return [text_response("👍 No changes made.")]

        return [text_response("👆 Pick an option from the Personal Info menu.")]

    # ─────────────────────────────────────────────────────────
    # STATE HANDLER — called by router when state == PERSONAL_INFO
    # ─────────────────────────────────────────────────────────

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle text input during a personal info flow."""
        context = session.get("context", {})
        step    = context.get("pi_step", "")
        text_s  = text.strip()

        if text_s.lower() in ("cancel", "exit", "back"):
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled. No changes made.")]

        # ── Business name ──
        if step == "edit_business_name":
            return self._save_field(
                phone_number, "business_name", text_s,
                f"✅ Business name updated to *{text_s}*!"
            )

        # ── Address ──
        if step == "edit_business_address":
            return self._save_field(
                phone_number, "business_address", text_s,
                f"✅ Address saved:\n_{text_s}_"
            )

        # ── Email ──
        if step == "edit_email":
            if not _valid_email(text_s):
                return [text_response(
                    "❌ That doesn't look like a valid email.\n\n"
                    "Try again or type *cancel*:"
                )]
            return self._save_field(
                phone_number, "email", text_s.lower(),
                f"✅ Email saved: {text_s.lower()}"
            )

        # ── Bank details — 3 steps ──
        if step == "bank_step_name":
            return self._bank_next(phone_number, context, "bank_name", text_s,
                                   "bank_step_account",
                                   "🔢 What is your *account number*?")

        if step == "bank_step_account":
            if not re.match(r'^\d{10}$', text_s.replace(" ", "")):
                return [text_response(
                    "❌ Account number must be exactly 10 digits.\n\n"
                    "Try again or type *cancel*:"
                )]
            clean = text_s.replace(" ", "")
            return self._bank_next(phone_number, context, "account_number", clean,
                                   "bank_step_acct_name",
                                   "👤 What is the *account name* on the account?\n\n"
                                   "_Exactly as it appears on your bank app_")

        if step == "bank_step_acct_name":
            return self._finish_bank_details(phone_number, context, text_s)

        # ── PIN — 2 steps ──
        if step == "pin_step_set":
            return self._pin_confirm_step(phone_number, context, text_s)

        if step == "pin_step_confirm":
            return self._pin_finish(phone_number, context, text_s)

        # Unknown step — reset
        self.session.reset(phone_number)
        return [text_response("Something went wrong. Please try again from the menu.")]

    # ─────────────────────────────────────────────────────────
    # SHOW — read-only views
    # ─────────────────────────────────────────────────────────

    def _show_field(self, phone_number: str, field: str) -> list:
        """Show current value of a field and offer to edit."""
        user = self.db.get_user(phone_number) or {}

        labels = {
            "business_name":    ("🏢 Business Name",    "business_name"),
            "business_address": ("📍 Business Address", "business_address"),
            "email":            ("📧 Email",            "email"),
        }
        label, key = labels.get(field, (field, field))
        current = user.get(key, "")
        value_line = f"Current: *{current}*" if current else "_Not set yet_"

        return [button_response(
            f"{label}\n\n{value_line}",
            [
                {"id": f"pi_{key.replace('business_', '')}_edit_confirm",
                 "title": "✏️ Edit"},
                {"id": "pi_cancel", "title": "← Back"},
            ]
        )]

    def _show_phone(self, phone_number: str) -> list:
        """Phone number is read-only — just display it."""
        return [text_response(
            f"📱 *Registered Phone Number*\n\n"
            f"*{phone_number}*\n\n"
            f"_This is your WhatsApp number and cannot be changed._"
        )]

    def _show_edit_menu(self, phone_number: str) -> list:
        """Show all editable fields."""
        user = self.db.get_user(phone_number) or {}
        name    = user.get("business_name", "—")
        bank    = user.get("bank_name", "")
        acct    = user.get("account_number", "")
        address = user.get("business_address", "")
        email   = user.get("email", "")
        has_pin = bool(user.get("pin_hash", ""))

        bank_desc    = f"{bank} · {acct}" if bank and acct else "Not set"
        address_desc = address[:40] if address else "Not set"
        email_desc   = email if email else "Not set"
        pin_desc     = "PIN is set ✅" if has_pin else "No PIN set"

        return [list_response(
            header="✏️ Edit Profile",
            body=f"*{name}* — tap a field to edit:",
            button_text="Select Field",
            sections=[{
                "title": "Profile Fields",
                "rows": [
                    {"id": "pi_business_name", "title": "🏢 Business Name",
                     "description": name[:40]},
                    {"id": "pi_bank",    "title": "🏦 Bank Details",
                     "description": bank_desc[:40]},
                    {"id": "pi_address", "title": "📍 Address",
                     "description": address_desc},
                    {"id": "pi_email",   "title": "📧 Email",
                     "description": email_desc},
                    {"id": "set_password", "title": "🔒 PIN / Password",
                     "description": pin_desc},
                ]
            }]
        )]

    def show_profile(self, phone_number: str) -> list:
        """Full profile view — called from pi_business_name or dashboard."""
        user = self.db.get_user(phone_number) or {}
        name     = user.get("business_name", "My Business")
        industry = user.get("business_type", user.get("industry_class", "trading")).title()
        tier     = user.get("tier", "free").capitalize()
        bank     = user.get("bank_name", "")
        acct     = user.get("account_number", "")
        acct_nm  = user.get("account_name", "")
        address  = user.get("business_address", "")
        email    = user.get("email", "")
        has_pin  = bool(user.get("pin_hash", ""))
        created  = user.get("created_at", "")[:10]

        lines = [
            f"👤 *{name}*",
            f"📱 {phone_number}",
            f"🏷️ {industry}  •  {tier} Plan",
        ]
        if created:
            lines.append(f"📅 Member since {created}")
        lines.append("")

        if bank or acct:
            lines.append("🏦 *Bank Details:*")
            if bank:    lines.append(f"  Bank: {bank}")
            if acct:    lines.append(f"  Account: {acct}")
            if acct_nm: lines.append(f"  Name: {acct_nm}")
            lines.append("")

        if address:
            lines.append(f"📍 {address}")
        if email:
            lines.append(f"📧 {email}")

        lines.append(f"🔒 PIN: {'Set ✅' if has_pin else 'Not set'}")

        return [
            text_response("\n".join(lines)),
            button_response(
                "What would you like to do?",
                [
                    {"id": "pi_edit",   "title": "✏️ Edit Profile"},
                    {"id": "pi_bank",   "title": "🏦 Bank Details"},
                    {"id": "set_password", "title": "🔒 Set PIN"},
                ]
            )
        ]

    # ─────────────────────────────────────────────────────────
    # BANK DETAILS — 3-step flow
    # ─────────────────────────────────────────────────────────

    def _start_bank_details(self, phone_number: str) -> list:
        """Start bank details collection."""
        user = self.db.get_user(phone_number) or {}
        bank    = user.get("bank_name", "")
        acct    = user.get("account_number", "")
        acct_nm = user.get("account_name", "")

        # Show current if exists
        if bank and acct:
            current = (
                f"🏦 *Current Bank Details:*\n"
                f"  Bank: {bank}\n"
                f"  Account: {acct}\n"
                f"  Name: {acct_nm}\n\n"
                f"Want to update them?"
            )
            return [button_response(
                current,
                [
                    {"id": "pi_bank_start_flow", "title": "✏️ Update"},
                    {"id": "pi_cancel",          "title": "← Keep Current"},
                ]
            )]

        return self._bank_step_1(phone_number)

    def _bank_step_1(self, phone_number: str) -> list:
        self.session.save(phone_number, PERSONAL_INFO_STATE, {
            "pi_step": "bank_step_name",
        })
        return [text_response(
            "🏦 *Bank Details Setup*\n\n"
            "Step 1 of 3\n\n"
            "What is your *bank name*?\n\n"
            "_e.g. Access Bank, GTB, First Bank, Zenith Bank, Opay, Palmpay_\n\n"
            "_Type *cancel* at any time to stop._"
        )]

    def _bank_next(self, phone_number: str, context: dict,
                   save_key: str, value: str,
                   next_step: str, next_prompt: str) -> list:
        """Save one bank field and move to the next step."""
        context[save_key] = value
        context["pi_step"] = next_step
        self.session.save(phone_number, PERSONAL_INFO_STATE, context)

        step_num = {"bank_step_account": 2, "bank_step_acct_name": 3}.get(next_step, 2)
        return [text_response(
            f"🏦 *Bank Details Setup*\n\n"
            f"Step {step_num} of 3\n\n"
            f"{next_prompt}"
        )]

    def _finish_bank_details(self, phone_number: str,
                              context: dict, acct_name: str) -> list:
        """Save all three bank fields to user profile."""
        bank_name      = context.get("bank_name", "")
        account_number = context.get("account_number", "")

        if not bank_name or not account_number:
            self.session.reset(phone_number)
            return [text_response(
                "❌ Something went wrong. Please try again from the menu."
            )]

        self.db.update_user(phone_number, {
            "bank_name":      bank_name,
            "account_number": account_number,
            "account_name":   acct_name.strip(),
        })
        self.session.reset(phone_number)

        return [text_response(
            f"✅ *Bank Details Saved!*\n\n"
            f"🏦 Bank: {bank_name}\n"
            f"🔢 Account: {account_number}\n"
            f"👤 Name: {acct_name.strip()}\n\n"
            f"_These details will appear on your invoices and receipts._"
        )]

    # ─────────────────────────────────────────────────────────
    # SINGLE-FIELD EDIT — address, email, business name
    # ─────────────────────────────────────────────────────────

    def _start_edit_field(self, phone_number: str, field: str,
                           title: str, prompt: str) -> list:
        """Start a single-field edit flow."""
        self.session.save(phone_number, PERSONAL_INFO_STATE, {
            "pi_step": f"edit_{field}",
        })
        return [text_response(
            f"{title}\n\n{prompt}\n\n_Type *cancel* to go back._"
        )]

    def _save_field(self, phone_number: str, field: str,
                    value: str, success_msg: str) -> list:
        """Save a single field to the user profile."""
        self.db.update_user(phone_number, {field: value})
        self.session.reset(phone_number)
        return [text_response(success_msg)]

    # ─────────────────────────────────────────────────────────
    # PIN / PASSWORD — 2-step (enter + confirm)
    # ─────────────────────────────────────────────────────────

    def _start_set_pin(self, phone_number: str) -> list:
        """Start PIN setup flow."""
        user    = self.db.get_user(phone_number) or {}
        has_pin = bool(user.get("pin_hash", ""))

        intro = (
            "🔒 *Change PIN*\n\nEnter your new 4-digit PIN:"
            if has_pin else
            "🔒 *Set a PIN*\n\n"
            "Your PIN protects your account.\n\n"
            "Enter a 4-digit PIN:"
        )
        self.session.save(phone_number, PERSONAL_INFO_STATE, {
            "pi_step": "pin_step_set",
        })
        return [text_response(f"{intro}\n\n_Type *cancel* to go back._")]

    def _pin_confirm_step(self, phone_number: str,
                           context: dict, text: str) -> list:
        """Validate format then ask for confirmation."""
        pin = text.strip()
        if not re.match(r'^\d{4}$', pin):
            return [text_response(
                "❌ PIN must be exactly 4 digits (e.g. 1234).\n\n"
                "Try again:"
            )]

        # Store hashed PIN temporarily in session — never store plain PIN
        context["pin_hash_temp"] = _hash_pin(pin)
        context["pi_step"]       = "pin_step_confirm"
        self.session.save(phone_number, PERSONAL_INFO_STATE, context)

        return [text_response(
            "🔒 *Confirm PIN*\n\n"
            "Type your 4-digit PIN again to confirm:"
        )]

    def _pin_finish(self, phone_number: str,
                    context: dict, text: str) -> list:
        """Verify confirmation matches and save."""
        pin              = text.strip()
        temp_hash        = context.get("pin_hash_temp", "")
        confirmation_hash = _hash_pin(pin)

        if temp_hash != confirmation_hash:
            # Mismatch — restart
            self.session.save(phone_number, PERSONAL_INFO_STATE, {
                "pi_step": "pin_step_set",
            })
            return [text_response(
                "❌ *PINs don't match.*\n\n"
                "Let's try again. Enter your 4-digit PIN:"
            )]

        self.db.update_user(phone_number, {"pin_hash": temp_hash})
        self.session.reset(phone_number)

        return [text_response(
            "✅ *PIN set successfully!*\n\n"
            "🔒 Your account is now protected.\n\n"
            "_You can change your PIN anytime from Help & Settings._"
        )]

    # ─────────────────────────────────────────────────────────
    # CONFIRM SAVE helper (for edit flows that use a button confirm)
    # ─────────────────────────────────────────────────────────

    def _confirm_save(self, phone_number: str) -> list:
        """Route confirm button back to the active edit step."""
        session = self.session.get(phone_number)
        context = session.get("context", {})
        step    = context.get("pi_step", "")

        # Map step to the appropriate start flow
        step_map = {
            "edit_business_name":    lambda: self._start_edit_field(
                phone_number, "business_name",
                "🏢 *Business Name*",
                "Type your new business name:"
            ),
            "edit_business_address": lambda: self._start_edit_field(
                phone_number, "business_address",
                "📍 *Business Address*",
                "Type your shop/market address:"
            ),
            "edit_email": lambda: self._start_edit_field(
                phone_number, "email",
                "📧 *Email Address*",
                "Type your email address:"
            ),
        }

        handler = step_map.get(step)
        if handler:
            return handler()

        # No active step — start fresh with business name
        return self._start_edit_field(
            phone_number, "business_name",
            "🏢 *Business Name*",
            "Type your new business name:"
        )


# ─────────────────────────────────────────────────────────
# MODULE HELPERS
# ─────────────────────────────────────────────────────────

def _hash_pin(pin: str) -> str:
    """SHA-256 hash of PIN — never store plain text PINs."""
    return hashlib.sha256(pin.encode()).hexdigest()


def _valid_email(text: str) -> bool:
    """Basic email format check."""
    return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', text))
