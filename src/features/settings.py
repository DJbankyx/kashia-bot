# src/features/settings.py
"""Help & Settings — tutorial, usage, upgrade, industry change, notifications, reset."""

import logging

from core import states
from utils.whatsapp_ui import (
    text_response, button_response, list_response
)

logger = logging.getLogger(__name__)

SETTINGS_STATE = "SETTINGS_FLOW"

# All industry options
INDUSTRY_OPTIONS = {
    "1": ("trading",       "🛍️ Trading & Retail",     "Buy and sell goods"),
    "2": ("services",      "🔧 Services",              "Consulting, repairs, skills"),
    "3": ("food",          "🍽️ Food & Drinks",         "Restaurant, catering, sales"),
    "4": ("manufacturing", "🏭 Manufacturing",         "Production, fabrication"),
    "5": ("hybrid",        "🔀 Hybrid",                "Products + services combined"),
}


class SettingsHandler:
    """Handles all Help & Settings section flows."""

    def __init__(self, session_mgr, database, tier_manager):
        self.session      = session_mgr
        self.db           = database
        self.tier_manager = tier_manager

    # ─────────────────────────────────────────────────────────
    # BUTTON ENTRY POINTS
    # ─────────────────────────────────────────────────────────

    def handle_button(self, phone_number: str, button_id: str) -> list:
        """Route all set_* buttons."""

        if button_id == "set_tutorial":
            return self._show_tutorial()

        if button_id == "set_usage":
            return self._show_usage(phone_number)

        if button_id == "set_upgrade":
            return self._show_upgrade(phone_number)

        if button_id == "set_industry":
            return self._start_change_industry(phone_number)

        if button_id.startswith("set_ind_"):
            # Tapping an industry option — was falling through to the default
            # "Pick an option" message (Change Industry did nothing). Route it to
            # the handler that actually saves the new industry.
            return self._finish_change_industry(phone_number, button_id)

        if button_id == "set_notify":
            return self._show_notifications(phone_number)

        if button_id == "set_bug":
            return self._show_bug_report()

        if button_id == "set_logo":
            return self._show_logo(phone_number)

        if button_id == "set_transfer":
            return self._show_transfer(phone_number)

        if button_id == "set_transfer_get":
            return self._issue_transfer_code(phone_number)

        if button_id == "set_transfer_claim":
            return self._start_transfer_claim(phone_number)

        if button_id == "set_reset":
            from core.pin_guard import requires_pin
            pin_check = requires_pin(self.db, self.session, phone_number, "set_reset")
            if pin_check:
                return pin_check
            return self._confirm_reset(phone_number)

        # Confirm buttons from within flows
        if button_id == "set_reset_yes":
            return self._execute_reset(phone_number)

        if button_id == "set_reset_no":
            self.session.reset(phone_number)
            return [text_response("👍 Your account data is safe.")]

        # ── HARD reset: wipe everything AND re-run onboarding (re-pick industry) ──
        if button_id in ("set_hardreset", "hard_reset"):
            from core.pin_guard import requires_pin
            pin_check = requires_pin(self.db, self.session, phone_number, "set_hardreset")
            if pin_check:
                return pin_check
            return self._confirm_hard_reset(phone_number)

        if button_id == "set_hardreset_yes":
            return self._execute_hard_reset(phone_number)

        if button_id == "set_hardreset_no":
            self.session.reset(phone_number)
            return [text_response("👍 Your account is safe. Nothing was deleted.")]

        if button_id == "set_notify_on":
            return self._set_notifications(phone_number, True)

        if button_id == "set_notify_off":
            return self._set_notifications(phone_number, False)

        # ── Preferences screen + real toggles (rebuild part B) ──
        if button_id in ("set_preferences", "menu_settings"):
            return self._show_preferences(phone_number)

        if button_id.startswith("set_pref_"):
            return self._toggle_pref(phone_number, button_id[len("set_pref_"):])

        if button_id == "set_costing":
            return self._show_costing(phone_number)

        if button_id == "set_costing_average":
            return self._set_costing_mode(phone_number, "average")

        if button_id == "set_costing_specific":
            return self._set_costing_mode(phone_number, "specific")

        if button_id.startswith("set_upgrade_"):
            # set_upgrade_<plan>            → show the period picker
            # set_upgrade_<plan>_<period>   → generate the payment link
            rest = button_id.replace("set_upgrade_", "")
            parts = rest.split("_")
            plan = parts[0]
            period = parts[1] if len(parts) > 1 else None
            return self._handle_upgrade_request(phone_number, plan, period)

        return [text_response("👆 Pick an option from the Settings menu.")]

    # ─────────────────────────────────────────────────────────
    # STATE HANDLER — called by router when state == SETTINGS_FLOW
    # ─────────────────────────────────────────────────────────

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle text input during a settings flow."""
        context  = session.get("context", {})
        step     = context.get("set_step", "")
        text_s   = text.strip()
        text_low = text_s.lower()

        if text_low in ("cancel", "exit", "back"):
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled.")]

        if step == "change_industry":
            return self._finish_change_industry(phone_number, text_s)

        if step == "transfer_claim":
            return self._finish_transfer_claim(phone_number, text_s)

        if step == "bug_report":
            return self._save_bug_report(phone_number, text_s)

        self.session.reset(phone_number)
        return [text_response("Something went wrong. Please try again.")]

    # ─────────────────────────────────────────────────────────
    # TUTORIAL
    # ─────────────────────────────────────────────────────────

    def _show_tutorial(self) -> list:
        """How to use Kashia — concise guide."""
        pages = [
            text_response(
                "📖 *How to Use Kashia* — Part 1 of 4\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📝 *Recording Transactions*\n\n"
                "Just type what happened naturally:\n\n"
                "  _\"sold 10 Nike shoes to Sandra 150K\"_\n"
                "  _\"bought 5 bags rice from Alhaji 95K\"_\n"
                "  _\"paid transport 5000\"_\n\n"
                "Kashia reads it, asks you to confirm, then saves.\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "💳 *Recording Credit Sales*\n\n"
                "  _\"Bola took goods worth 20K on credit\"_\n"
                "  _\"I owe Dangote 50K for flour\"_\n\n"
                "Kashia tracks the debt automatically."
            ),
            text_response(
                "📖 *How to Use Kashia* — Part 2 of 4\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📊 *Reports*\n\n"
                "Tap *Business → Reports* or type:\n"
                "  • _\"report\"_ — this month's P&L\n"
                "  • _\"today\"_ — today only\n"
                "  • _\"this week\"_ — last 7 days\n\n"
                "The report shows:\n"
                "  💰 Revenue  −  📦 Cost of goods sold\n"
                "  =  📊 Gross profit  −  💸 Expenses\n"
                "  =  📈 Net profit\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "💳 *Debt Tracking*\n\n"
                "  • Tap *Business → Debts & Credits*\n"
                "  • Or type _\"who owes me\"_\n"
                "  • Record payment: _\"Sandra paid me 10K\"_"
            ),
            text_response(
                "📖 *How to Use Kashia* — Part 3 of 4\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📦 *Product Catalog*\n\n"
                "Set up your products once:\n"
                "  Tap *Business → Product Catalog*\n\n"
                "Once set up, recording is faster —\n"
                "Kashia knows your products and formats.\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "🧾 *Invoices & Receipts*\n\n"
                "  Tap *Business → Documents*\n"
                "  → Generate Invoice / Receipt / Statement\n\n"
                "Sent as PDF via WhatsApp.\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "❓ Type *help* at any time to see this menu again."
            ),
            text_response(
                "📖 *How to Use Kashia* — Part 4 of 4\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "💡 *Getting your money right*\n\n"
                "Kashia sorts your money into 3 buckets. "
                "Putting things in the right bucket keeps your "
                "profit honest:\n\n"
                "📦 *Stock you buy to resell* (or raw materials)\n"
                "  Record it as a *purchase*. It is NOT an expense — "
                "it becomes inventory. Its cost only hits profit "
                "when you actually *sell* it (as \"cost of goods "
                "sold\").\n\n"
                "💸 *Running costs* — rent, fuel, data, salaries, "
                "transport, repairs.\n"
                "  These are *expenses*. They reduce this period's "
                "profit.\n\n"
                "🏗️ *Big things you buy to KEEP and use* — a "
                "vehicle, a machine, a generator, furniture.\n"
                "  These are not everyday expenses; they are assets "
                "you keep for years. For now, record the *cash* you "
                "paid, but know it isn't a normal running cost — a "
                "full asset/depreciation view is coming.\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "💵 *How cash flow is calculated*\n\n"
                "*Cash flow = money that actually moved.*\n"
                "  IN:  cash & transfer sales + deposits you "
                "received + debts collected.\n"
                "  OUT: cash you paid for stock, expenses & "
                "deposits + debts you repaid.\n\n"
                "Credit doesn't count until it's paid. So a big "
                "stock purchase shows as cash *out* now, but only "
                "lowers *profit* later, when the goods sell. That's "
                "why cash flow and profit can differ — both are "
                "correct, they answer different questions."
            ),
        ]
        return pages

    # ─────────────────────────────────────────────────────────
    # USAGE & LIMITS
    # ─────────────────────────────────────────────────────────

    def _show_usage(self, phone_number: str) -> list:
        """Show tier usage stats."""
        summary = self.tier_manager.get_usage_summary(phone_number)
        user    = self.db.get_user(phone_number) or {}
        tier    = user.get("tier", "free")

        responses = [text_response(summary)]

        # Nudge free users to upgrade
        if tier == "free":
            responses.append(button_response(
                "Want unlimited transactions and more?",
                [
                    {"id": "set_upgrade_basic", "title": "💼 Go Basic ₦3,500"},
                    {"id": "set_upgrade_pro",   "title": "🏆 Go Pro ₦6,500"},
                ]
            ))

        return responses

    # ─────────────────────────────────────────────────────────
    # UPGRADE PLAN
    # ─────────────────────────────────────────────────────────

    def _show_upgrade(self, phone_number: str) -> list:
        """Show upgrade options."""
        user = self.db.get_user(phone_number) or {}
        tier = user.get("tier", "free")

        plan_text = self.tier_manager.get_upgrade_options()

        responses = [text_response(plan_text)]

        if tier == "free":
            responses.append(button_response(
                "Choose your plan:",
                [
                    {"id": "set_upgrade_basic", "title": "💼 Basic — from ₦3,500/mo"},
                    {"id": "set_upgrade_pro",   "title": "🏆 Pro — from ₦6,500/mo"},
                ]
            ))
        elif tier == "basic":
            responses.append(button_response(
                "You're on Basic. Upgrade to Pro?",
                [
                    {"id": "set_upgrade_pro", "title": "🏆 Upgrade to Pro"},
                ]
            ))
        else:
            responses.append(text_response("✅ You're already on the Pro plan!"))

        return responses

    def _handle_upgrade_request(self, phone_number: str, plan: str, period=None) -> list:
        """Show the period picker (period=None) or generate the payment link."""
        result = self.tier_manager.handle_upgrade_request(phone_number, plan, period)
        return result

    # ─────────────────────────────────────────────────────────
    # CHANGE INDUSTRY
    # ─────────────────────────────────────────────────────────

    def _start_change_industry(self, phone_number: str) -> list:
        """Show industry options."""
        user = self.db.get_user(phone_number) or {}
        current = user.get(
            "industry_class",
            user.get("business_type", "trading")
        )

        rows = []
        for num, (key, label, desc) in INDUSTRY_OPTIONS.items():
            title = f"{'✅ ' if key == current else ''}{label}"
            rows.append({"id": f"set_ind_{key}", "title": title[:24], "description": desc})

        self.session.save(phone_number, SETTINGS_STATE, {
            "set_step": "change_industry",
        })

        return [list_response(
            header="🔄 Change Industry",
            body=f"Current: *{current.title()}*\n\nSwitch to a different business type:",
            button_text="Select",
            sections=[{"title": "Industry Types", "rows": rows}]
        )]

    def _finish_change_industry(self, phone_number: str, text: str) -> list:
        """Handle industry selection from list or text input."""
        text_low = text.lower().strip()

        # Came from a list button (set_ind_trading etc)
        if text_low.startswith("set_ind_"):
            new_industry = text_low.replace("set_ind_", "")
        else:
            # Try to match typed text to an industry
            new_industry = None
            for num, (key, label, desc) in INDUSTRY_OPTIONS.items():
                if text_low in (num, key, key.lower(), label.lower()):
                    new_industry = key
                    break

        if not new_industry or new_industry not in [v[0] for v in INDUSTRY_OPTIONS.values()]:
            return [text_response(
                "❌ Didn't recognise that. Please pick from the list."
            )]

        # Save to both fields for backward compatibility
        self.db.update_user(phone_number, {
            "industry_class": new_industry,
            "business_type":  new_industry,
        })
        self.session.reset(phone_number)

        label = next(
            (v[1] for v in INDUSTRY_OPTIONS.values() if v[0] == new_industry),
            new_industry.title()
        )
        return [text_response(
            f"✅ *Industry updated to {label}!*\n\n"
            f"_Your menu and reports will now reflect your business type._\n\n"
            f"_Tap the menu button to see your updated home screen._"
        )]

    # ─────────────────────────────────────────────────────────
    # NOTIFICATIONS
    # ─────────────────────────────────────────────────────────

    def _show_notifications(self, phone_number: str) -> list:
        """Show notification toggle."""
        user      = self.db.get_user(phone_number) or {}
        daily_on  = user.get("notify_daily",  True)
        weekly_on = user.get("notify_weekly", True)

        daily_label  = "✅ Daily Report:  ON"  if daily_on  else "❌ Daily Report:  OFF"
        weekly_label = "✅ Weekly Report: ON" if weekly_on else "❌ Weekly Report: OFF"

        return [button_response(
            f"🔔 *Notifications*\n\n"
            f"{daily_label}\n"
            f"{weekly_label}\n\n"
            f"_Daily reports arrive at 7PM every day._\n"
            f"_Weekly reports arrive Sunday evenings._",
            [
                {"id": "set_notify_on",  "title": "🔔 Turn All ON"},
                {"id": "set_notify_off", "title": "🔕 Turn All OFF"},
            ]
        )]

    def _set_notifications(self, phone_number: str, on: bool) -> list:
        """Toggle all notifications on or off."""
        self.db.update_user(phone_number, {
            "notify_daily":  on,
            "notify_weekly": on,
        })
        self.session.reset(phone_number)
        status = "ON 🔔" if on else "OFF 🔕"
        return [text_response(
            f"✅ Notifications turned *{status}*\n\n"
            f"_You can change this anytime from Help & Settings._"
        )]

    # ─────────────────────────────────────────────────────────
    # COSTING METHOD (weighted-average vs specific-identification)
    # ─────────────────────────────────────────────────────────

    def _show_costing(self, phone_number: str) -> list:
        """Show the costing-method choice. Default is weighted-average."""
        user = self.db.get_user(phone_number) or {}
        mode = str(user.get("costing_mode", "average")).lower()
        avg_mark = "✅ " if mode != "specific" else ""
        spec_mark = "✅ " if mode == "specific" else ""
        return [button_response(
            "🧮 *Costing method*\n\n"
            "How should I value the cost of what you sell?\n\n"
            f"{avg_mark}*Weighted average* — blends each restock into one running "
            "cost. Best for everyday stock with changing prices.\n\n"
            f"{spec_mark}*Specific* — costs each sale at the exact cost of that "
            "unit. Best for unique high-value items (e.g. vehicles).\n\n"
            "_Either way, each sale's cost is locked in when it's recorded — a "
            "later restock never changes past profit._",
            [
                {"id": "set_costing_average", "title": "⚖️ Weighted average"},
                {"id": "set_costing_specific", "title": "🎯 Specific"},
            ]
        )]

    def _set_costing_mode(self, phone_number: str, mode: str) -> list:
        """Persist the costing mode (average | specific)."""
        mode = "specific" if mode == "specific" else "average"
        self.db.update_user(phone_number, {"costing_mode": mode})
        self.session.reset(phone_number)
        label = "Specific (per-unit) 🎯" if mode == "specific" else "Weighted average ⚖️"
        return [text_response(
            f"✅ Costing method set to *{label}*.\n\n"
            f"_Applies to sales recorded from now on. Change anytime in "
            f"Help & Settings._"
        )]

    # ─────────────────────────────────────────────────────────
    # PREFERENCES — real in-place toggles (settings rebuild, part B)
    # ─────────────────────────────────────────────────────────

    def _is_telegram(self, phone_number: str) -> bool:
        try:
            from services.messaging_client import platform_for_user
            return platform_for_user(phone_number) == "telegram"
        except Exception:
            return False

    # Preference flags: user-record key + default (default True = on).
    _PREF_FLAGS = {
        "daily":        ("notify_daily", True),
        "weekly":       ("notify_weekly", True),
        "insightsnudge": ("insights_nudge", True),
    }

    @staticmethod
    def _on(v, default=True):
        """Interpret a stored flag as bool (handles True/'true'/None/missing)."""
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        return str(v).lower() not in ("false", "0", "no", "off")

    def _show_preferences(self, phone_number: str) -> list:
        """The in-place toggle screen. Each row shows its live ON/OFF state and
        flips when tapped (re-rendering this same card). Telegram tap-first;
        WhatsApp falls back to the simple all-on/all-off notification buttons."""
        user = self.db.get_user(phone_number) or {}

        if not self._is_telegram(phone_number):
            # WhatsApp keeps the simpler coupled control.
            return self._show_notifications(phone_number)

        def _row(key, on_title, off_title, desc):
            fkey, dflt = self._PREF_FLAGS[key]
            on = self._on(user.get(fkey), dflt)
            mark = "✅" if on else "⬜"
            return {"id": f"set_pref_{key}",
                    "title": f"{mark} {on_title if on else off_title}"[:60],
                    "description": desc}

        mode = str(user.get("costing_mode", "average")).lower()
        costing_label = "🎯 Specific" if mode == "specific" else "⚖️ Weighted avg"

        rows = [
            _row("daily", "Daily report: ON", "Daily report: OFF",
                 "7PM daily summary"),
            _row("weekly", "Weekly report: ON", "Weekly report: OFF",
                 "Sunday evening overview"),
            _row("insightsnudge", "Smart Insights nudge: ON",
                 "Smart Insights nudge: OFF", "Weekly AI business tip (Pro)"),
            {"id": "set_costing", "title": f"🧮 Costing: {costing_label}",
             "description": "How cost of sales is valued"},
            {"id": "sec_settings", "title": "⬅️ Back to Settings"},
        ]
        return [list_response(
            header="⚙️ Preferences",
            body="Tap a switch to turn it on or off.",
            button_text="Toggle",
            sections=[{"title": "", "rows": rows}],
            no_paginate=True,
        )]

    def _toggle_pref(self, phone_number: str, key: str) -> list:
        """Flip one preference flag, then re-render the Preferences screen."""
        spec = self._PREF_FLAGS.get(key)
        if not spec:
            return self._show_preferences(phone_number)
        fkey, dflt = spec
        user = self.db.get_user(phone_number) or {}
        new_val = not self._on(user.get(fkey), dflt)
        try:
            self.db.update_user(phone_number, {fkey: new_val})
        except Exception as e:
            logger.warning(f"toggle {key} failed: {e}")
        return self._show_preferences(phone_number)

    # ─────────────────────────────────────────────────────────
    # SETTINGS HOME — grouped (settings rebuild, part A)
    # ─────────────────────────────────────────────────────────

    def show_settings(self, phone_number: str) -> list:
        """Grouped Settings home (Telegram tap-first): Account / Preferences /
        Help / Danger Zone. WhatsApp keeps the industry's flat list."""
        if not self._is_telegram(phone_number):
            # Let the industry render its classic settings list on WhatsApp.
            industry = None
            try:
                if getattr(self, "router", None):
                    industry = self.router._get_industry_handler(phone_number)
            except Exception:
                industry = None
            if industry and hasattr(industry, "_show_settings_menu"):
                return industry._show_settings_menu(phone_number)
            # Fallback minimal list.
            return [text_response("⚙️ Settings — type *menu* to go back.")]

        return [list_response(
            header="⚙️ Settings",
            body="Manage your account, preferences and data.",
            button_text="Open",
            sections=[
                {"title": "👤 Account", "rows": [
                    {"id": "set_usage", "title": "📊 Usage & Limits",
                     "description": "Your tier & what's left this month"},
                    {"id": "set_upgrade", "title": "⭐ Upgrade Plan",
                     "description": "Free → Basic → Pro"},
                    {"id": "set_industry", "title": "🔄 Change Industry",
                     "description": "Switch business type"},
                    {"id": "set_password", "title": "🔒 Set / Change PIN",
                     "description": "Protect sensitive actions"},
                    {"id": "set_logo", "title": "🖼️ Business Logo",
                     "description": "Shown on invoices & receipts"},
                    {"id": "set_transfer", "title": "🔁 Transfer / Recover",
                     "description": "Move your data to a new phone/Telegram"},
                ]},
                {"title": "⚙️ Preferences", "rows": [
                    {"id": "set_preferences", "title": "🔔 Notifications & Toggles",
                     "description": "Daily/weekly reports, insights nudge, costing"},
                ]},
                {"title": "❓ Help", "rows": [
                    {"id": "set_tutorial", "title": "❓ How to Use",
                     "description": "Quick guide & tutorial"},
                    {"id": "set_bug", "title": "🐛 Report a Problem",
                     "description": "Send feedback"},
                ]},
                {"title": "🗑️ Danger Zone", "rows": [
                    {"id": "set_reset", "title": "🧹 Clear My Data",
                     "description": "Wipe transactions, contacts & catalog (keep account)"},
                    {"id": "set_hardreset", "title": "🗑️ Full Reset",
                     "description": "Delete everything & start onboarding over"},
                ]},
            ],
        )]

    # ─────────────────────────────────────────────────────────
    # REPORT A BUG
    # ─────────────────────────────────────────────────────────

    def _show_bug_report(self) -> list:
        """Give user a way to report bugs / send feedback — and CAPTURE it."""
        self.session.save(phone_number, SETTINGS_STATE, {"set_step": "bug_report"})
        return [text_response(
            "🐛 *Report a Problem / Send Feedback*\n\n"
            "Type your message here and I'll log it for the team.\n\n"
            "Or reach us directly:\n"
            "📧 support@kashia.app\n\n"
            "_Common fixes:_\n"
            "• If the bot is stuck, type *cancel*\n"
            "• If a transaction was wrong, type *undo*\n"
            "• If the menu disappeared, type */menu*"
        )]

    def _save_bug_report(self, phone_number: str, message: str) -> list:
        """Persist a user's feedback/bug report so the team can review it."""
        self.session.reset(phone_number)
        msg = (message or "").strip()
        if not msg:
            return [text_response("No message received. Type */menu* to go back.")]
        try:
            # Reuse the feedback table as a lightweight support log.
            self.db.save_feedback(phone_number, f"[SUPPORT] {msg}", "", "")
        except Exception as e:
            logger.warning(f"save_bug_report failed: {e}")
        return [text_response(
            "✅ Thanks — your message has been logged and the team will look "
            "into it.\n\n"
            "_For anything urgent, email support@kashia.app._\n\n"
            "Type */menu* to continue."
        )]

    def _show_logo(self, phone_number: str) -> list:
        """Explain how to set the business logo shown on invoices & receipts.

        The logo pipeline already exists (a photo sent with no scan caption is
        stored as logo_s3_key and drawn on every invoice/receipt PDF). It was
        just undiscoverable — this makes it an explicit, guided action."""
        user = self.db.get_user(phone_number) or {}
        has_logo = bool(user.get("logo_s3_key"))
        status = ("✅ A logo is set — it appears on your invoices & receipts."
                  if has_logo else
                  "ℹ️ No logo set yet.")
        return [text_response(
            "🖼️ *Business Logo*\n\n"
            f"{status}\n\n"
            "*To add or change it:* just send me a *photo* of your logo "
            "right here (as a normal photo, no caption).\n\n"
            "_Tip: a clear square image works best. It's placed at the top "
            "of every invoice and receipt PDF._"
        )]

    # ─────────────────────────────────────────────────────────
    # ACCOUNT TRANSFER / RECOVERY
    # ─────────────────────────────────────────────────────────

    def _show_transfer(self, phone_number: str) -> list:
        """Explain the two sides of an account move: get a code on the OLD
        device, enter it on the NEW device."""
        return [button_response(
            "🔁 *Transfer / Recover Your Account*\n\n"
            "Moving to a new phone or Telegram? You can move ALL your data — "
            "sales, stock, customers, debts — to the new one.\n\n"
            "*On your OLD/current device:* tap *Get a code* below.\n"
            "*On the NEW device:* open Kashia there, go to Settings → "
            "Transfer / Recover → *Enter a code*, and type it in.\n\n"
            "_A code lasts 30 minutes and works once._",
            [
                {"id": "set_transfer_get", "title": "🔑 Get a code (this device)"},
                {"id": "set_transfer_claim", "title": "📥 Enter a code"},
            ]
        )]

    def _issue_transfer_code(self, phone_number: str) -> list:
        """Generate + show a one-time transfer code for THIS account."""
        code = self.db.issue_transfer_code(phone_number, ttl_minutes=30)
        if not code:
            return [text_response("⚠️ Couldn't create a code right now. Please try again.")]
        return [text_response(
            f"🔑 *Your transfer code:*\n\n"
            f"        *{code}*\n\n"
            f"On your NEW device, open Kashia → Settings → Transfer / Recover → "
            f"*Enter a code*, and type this in.\n\n"
            f"_Valid for 30 minutes. Works once. Don't share it — anyone with "
            f"this code can claim your data._"
        )]

    def _start_transfer_claim(self, phone_number: str) -> list:
        """Ask the NEW device to type the code from the old device."""
        self.session.save(phone_number, SETTINGS_STATE, {"set_step": "transfer_claim"})
        return [text_response(
            "📥 *Enter your transfer code*\n\n"
            "Type the 6-character code you got on your OLD device.\n\n"
            "_Type cancel to stop._"
        )]

    def _finish_transfer_claim(self, phone_number: str, code: str) -> list:
        """Redeem a transfer code on the NEW device → move the old account here.

        Safety: refuse if THIS account already has real data (so a transfer can't
        silently overwrite an active account). The code must be valid+unexpired.
        """
        self.session.reset(phone_number)
        old_id = self.db.find_user_by_transfer_code(code)
        if not old_id:
            return [text_response(
                "❌ That code is invalid or has expired. Get a fresh one on your "
                "old device (Settings → Transfer / Recover → Get a code)."
            )]
        if old_id == phone_number:
            return [text_response("That code belongs to THIS device already — "
                                  "no transfer needed.")]

        # Guard: don't clobber an already-active account on this new id.
        me = self.db.get_user(phone_number) or {}
        if int(me.get("transaction_count", 0) or 0) > 0 or me.get("product_catalog"):
            return [text_response(
                "⚠️ This device already has business data. Transferring would "
                "overwrite it. For safety I stopped.\n\n"
                "_If you really want to move the other account here, clear this "
                "one first (Settings → Clear My Data), then enter the code again._"
            )]

        try:
            result = self.db.transfer_account(old_id, phone_number)
            self.db.clear_transfer_code(phone_number)  # code lived on old row; now moved
            self.db.clear_transfer_code(old_id)
        except Exception as e:
            logger.error(f"transfer claim failed: {e}")
            return [text_response("⚠️ The transfer hit a problem. Nothing was "
                                  "lost on your old device — please try again.")]

        moved = int(result.get("transactions", 0))
        return [text_response(
            f"✅ *Account moved to this device!*\n\n"
            f"Your data is here now — {moved} transaction(s), plus your catalog, "
            f"customers and debts.\n\n"
            f"_Type /menu to see your dashboard._"
        )]

    # ─────────────────────────────────────────────────────────
    # RESET ACCOUNT
    # ─────────────────────────────────────────────────────────

    def _confirm_reset(self, phone_number: str) -> list:
        """Show reset warning with confirmation buttons."""
        return [button_response(
            "🧹 *Clear My Data*\n\n"
            "This clears from your account:\n"
            "  • All your transactions\n"
            "  • All contacts & debts\n"
            "  • Your product catalog\n"
            "  • All reports\n\n"
            "*Your account login will remain.*\n\n"
            "_For your protection, financial records are kept securely and\n"
            "hidden from your account for the legal retention period, then\n"
            "removed automatically._\n\n"
            "Are you sure?",
            [
                {"id": "set_reset_yes", "title": "🗑️ Yes, Clear It"},
                {"id": "set_reset_no",  "title": "← Keep My Data"},
            ]
        )]

    def _execute_reset(self, phone_number: str) -> list:
        """Clear the user's data from view — ARCHIVES it (soft-delete) rather
        than physically deleting, so records stay recoverable/auditable for the
        retention window (compliance). To the user the effect is identical: their
        transactions, contacts and catalog vanish from every screen and report.
        True erasure is a separate, deliberate admin action (see audit tool)."""
        try:
            # 1. Archive all transactions (soft-delete — recoverable)
            self.db.archive_all_transactions(phone_number)

            # 2. Archive all contacts (soft-delete — recoverable)
            self.db.archive_all_contacts(phone_number)

            # 3. Wipe catalog, debts, sessions from user record
            self.db.update_user(phone_number, {
                "product_catalog":         {},
                "transaction_count":       0,
                "exports_this_month":      0,
                "invoices_this_month":     0,
                "last_deleted_transaction": None,
            })

            # 4. Reset session
            self.session.reset(phone_number)

            logger.info(f"Account reset executed for {phone_number}")

            return [text_response(
                "🗑️ *Account reset complete.*\n\n"
                "All transactions, contacts, and catalog data have been deleted.\n\n"
                "Your account is still active. Type *hi* to start fresh."
            )]

        except Exception as e:
            logger.error(f"Reset error for {phone_number}: {e}")
            self.session.reset(phone_number)
            return [text_response(
                "❌ Reset failed. Please try again or contact support."
            )]

    # ─────────────────────────────────────────────────────────
    # HARD RESET — wipe data AND force fresh onboarding (re-pick industry)
    # ─────────────────────────────────────────────────────────

    def _confirm_hard_reset(self, phone_number: str) -> list:
        """Show the hard-reset warning with confirmation buttons."""
        return [button_response(
            "⚠️ *Full Reset (start over)*\n\n"
            "This clears *everything* and starts you fresh:\n"
            "  • All transactions\n"
            "  • All contacts & debts\n"
            "  • Your product catalog\n"
            "  • Your business profile & industry\n\n"
            "You'll begin again from onboarding and pick your industry fresh.\n\n"
            "_For your protection, financial records are kept securely and\n"
            "hidden from your account for the legal retention period, then\n"
            "removed automatically._\n\n"
            "Are you sure?",
            [
                {"id": "set_hardreset_yes", "title": "🗑️ Yes, Start Over"},
                {"id": "set_hardreset_no",  "title": "← Keep My Data"},
            ]
        )]

    def _execute_hard_reset(self, phone_number: str) -> list:
        """Clear all data from view and reset onboarding so the user re-onboards
        fresh.

        Like _execute_reset, this ARCHIVES transactions + contacts (soft-delete)
        rather than physically deleting them, so financial records stay
        recoverable/auditable for the retention window (compliance: 6-year
        FIRS/tax minimum). No user-facing action ever hard-deletes; true erasure
        happens only via the time-based retention purge or a deliberate admin
        action (audit tool). Unlike _execute_reset (which keeps the profile +
        industry), this ALSO clears the fields the router uses to decide a user
        is 'known', so the next message drops them into onboarding from step one
        (industry re-pick).
        """
        try:
            # Archive (soft-delete) — recoverable for the retention window.
            self.db.archive_all_transactions(phone_number)
            self.db.archive_all_contacts(phone_number)

            # Clear the profile fields that mark the user as onboarded. The
            # router's _user_exists() returns False when onboarding_complete is
            # falsy, which routes the next message into onboarding.
            self.db.update_user(phone_number, {
                "onboarding_complete":  False,
                "business_name":        "",
                "industry_class":       "",
                "business_type":        "",
                "product_catalog":      {},
                "transaction_count":    0,
                "exports_this_month":   0,
                "invoices_this_month":  0,
                "recurring_services":   [],
                "last_deleted_transaction": None,
            })

            # Fully clear the session so no stale state lingers.
            self.session.reset(phone_number)

            logger.info(f"HARD reset executed for {phone_number}")

            return [text_response(
                "🗑️ *Full reset complete.*\n\n"
                "Everything has been cleared. Let's set you up fresh.\n\n"
                "👉 Type *hi* (or tap /start) to begin onboarding."
            )]
        except Exception as e:
            logger.error(f"Hard reset error for {phone_number}: {e}")
            self.session.reset(phone_number)
            return [text_response(
                "❌ Reset failed. Please try again or contact support."
            )]

    # NOTE: the old physical batch-delete helpers (_delete_all_transactions /
    # _delete_all_contacts) were removed. No user-facing action hard-deletes
    # records anymore — both "Clear My Data" and "Full Reset" ARCHIVE
    # (soft-delete) so financial records survive for the 6-year retention window
    # (compliance). True erasure happens only via the scheduled retention purge
    # (past retain_until) or a deliberate admin action (scripts/audit_user.py
    # --edit purge → db.purge_user_records).
