# src/core/button_dispatcher.py
"""ButtonDispatcher — routes interactive button/list taps to feature handlers.

Extracted from Router._route_button to keep the router focused on the state
machine. Behavior is identical: the dispatcher holds a reference back to the
Router and calls into its feature handlers and helpers.
"""

import logging

from core import states
from utils.whatsapp_ui import text_response

logger = logging.getLogger(__name__)


class ButtonDispatcher:
    """Maps a button/list tap to the correct feature handler."""

    def __init__(self, router):
        self.router = router

    def dispatch(self, phone_number: str, button_id: str, session: dict) -> list:
        """Route interactive button/list taps to the correct handler."""
        r = self.router
        bid = button_id.lower().strip()
        state = session.get("state", states.IDLE)

        logger.info(f"Button route: {bid} (state={state})")

        # ── Confirmation buttons (from transaction confirmation card) ──
        if bid in ("confirm_yes", "yes", "✅ yes"):
            if state == states.AWAITING_CONFIRMATION:
                return r.transactions.handle_confirmation(phone_number, "yes", session)
            if state == states.DEBT_CONFIRMING:
                return r.debt.handle(phone_number, "yes", session)

        if bid in ("confirm_edit", "edit", "✏️ edit"):
            if state == states.AWAITING_CONFIRMATION:
                return r.transactions.handle_confirmation(phone_number, "edit", session)

        if bid in ("confirm_cancel", "btn_cancel", "cancel", "❌ cancel"):
            r.session.reset(phone_number)
            return [text_response("❌ Cancelled. Send a transaction or tap the menu.")]

        # ── Edit field selection (from pending transaction correction flow) ──
        if bid.startswith("edit_") and state == states.AWAITING_CORRECTION:
            return r.transactions.handle_correction(phone_number, bid, session)

        # ── Done button ──
        if bid == "btn_done":
            if state in (states.CATALOG_SETUP_DETAILS, states.CATALOG_ORGANIZE,
                         states.CATALOG_ADD_DATA, states.CATALOG_SETUP_PRODUCTS):
                return r.catalog.handle(phone_number, "done", session)

        # ── Back button ──
        if bid == "btn_back":
            if state == states.GUIDED_RECORDING:
                return r.transactions.handle_guided_step(phone_number, "__BACK__", session)

        # ── Yes/No buttons ──
        if bid == "btn_yes":
            if state == states.DEBT_CONFIRMING:
                return r.debt.handle(phone_number, "yes", session)
            if state == states.DELETE_CONFIRM:
                return r.transactions.handle_edit(phone_number, "yes", session)

        if bid == "btn_no":
            if state == states.DEBT_CONFIRMING:
                return r.debt.handle(phone_number, "no", session)
            r.session.reset(phone_number)
            return [text_response("👍 Okay. Send a transaction or tap the menu.")]

        # ── Recording buttons (from home menu) ──
        if bid.startswith("record_"):
            return r._start_guided_recording(phone_number, bid)

        # ── Feature menu buttons ──
        feature_map = {
            "menu_report": lambda: r.reports.show(phone_number),
            "menu_profile": lambda: r.profile.show(phone_number),
            "menu_catalog": lambda: r.catalog.show_menu(phone_number),
            "menu_debts": lambda: r.debt.show_summary(phone_number),
            "menu_contacts": lambda: r.contacts.show(phone_number),
            "menu_export": lambda: r.export.show_options(phone_number),
            "menu_invoice": lambda: r.invoices.start(phone_number),
            "menu_home": lambda: r._show_home_menu(phone_number),
        }

        handler = feature_map.get(bid)
        if handler:
            # Reset state when navigating to menu (user is leaving current flow)
            if bid == "menu_home":
                r.session.reset(phone_number)
            return handler()

        # ── Telegram: open the clean tap-first CRM home (sec_crm / menu_contacts) ──
        # Must run BEFORE the industry handler so it doesn't return the old
        # list-style CRM menu. WhatsApp keeps the industry menu.
        if bid in ("sec_crm", "menu_contacts") and r.contacts is not None:
            try:
                from services.messaging_client import platform_for_user
                if platform_for_user(phone_number) == "telegram":
                    return r.contacts.crm_home(phone_number)
            except Exception:
                pass

        # ── Industry-specific buttons ──
        industry = r._get_industry_handler(phone_number)
        if industry:
            result = industry.handle_button(phone_number, bid, session)
            if result:
                return result

        # ── CRM buttons ──
        if bid.startswith("crm_"):
            # CRM hint buttons (cash/transfer/credit) from transaction flow
            if bid in ("crm_cash", "crm_transfer", "crm_credit"):
                return r._handle_crm_button(phone_number, bid, session)
            # CRM type selection during add contact flow
            if bid.startswith("crm_type_"):
                session = r.session.get(phone_number)
                return r.contacts.handle(phone_number, bid, session)
            # All other crm_ buttons → contacts handler
            return r.contacts.handle_button(phone_number, bid, session)

        # ── Catalog buttons ──
        if bid.startswith("cat_"):
            return r.catalog.handle_button(phone_number, bid, session)

        # ── Catalog Recording buttons (catrec_*) ──
        if bid.startswith("catrec_"):
            return r.transactions.handle_catalog_recording(phone_number, bid, session)

        # ── Transaction Edit buttons (txedit_* and txact_*) ──
        if bid.startswith("txedit_") or bid.startswith("txact_"):
            return r.transactions.handle_edit_button(phone_number, bid, session)

        # ── Landing Cost buttons (lc_*) ──
        if bid.startswith("lc_"):
            return r.transactions.handle_landing_cost(phone_number, bid, session)

        # ── Variant Selection buttons (var_*) ──
        if bid.startswith("var_"):
            return r.transactions.handle_variant_selection(phone_number, bid, session)

        # ── Payment Method buttons (pm_*) ──
        if bid.startswith("pm_"):
            return r.transactions.handle_payment_method(phone_number, bid, session)

        # ── Generate Invoice/Receipt after sale (gen_*) ──
        if bid.startswith("gen_invoice_"):
            tx_id = bid[12:]
            return [{"type": "__GEN_INVOICE__", "content": {"tx_id": tx_id}}]
        if bid.startswith("gen_receipt_"):
            tx_id = bid[12:]
            return [{"type": "__GEN_RECEIPT__", "content": {"tx_id": tx_id}}]
        if bid == "gen_skip":
            return [text_response("👍 _Send your next transaction or tap ☰ Menu._")]

        # ── Expense classification buttons (manufacturing) ──
        if bid.startswith("expclass_"):
            return r._handle_expense_class(phone_number, bid)

        # ── Production buttons (prod_*) ──
        if bid.startswith("prod_"):
            return r.production.handle_button(phone_number, bid, session)

        # ── Recurring service buttons (rec_*) ──
        if bid.startswith("rec_"):
            return r.recurring.handle_button(phone_number, bid)

        # ── Quote buttons (quote_*) ──
        if bid.startswith("quote_"):
            return r.quotes.handle_button(phone_number, bid)

        # ── Debt buttons ──
        if bid.startswith("debt_"):
            return r.debt.handle_button(phone_number, bid, session)

        # ── Report buttons ──
        if bid.startswith("report_"):
            return r.reports.handle_button(phone_number, bid, session)

        # ── Section sub-menu buttons (industry-specific) ──
        if bid.startswith("sec_") or bid.startswith("pi_") or bid.startswith("biz_") or bid.startswith("crm_") or bid.startswith("set_"):
            # Try industry handler first
            industry = r._get_industry_handler(phone_number)
            if industry:
                result = industry.handle_button(phone_number, bid, session)
                if result:
                    return result

            # ── sec_personal → show profile summary + sub-menu ──
            if bid == "sec_personal" and r.personal_info:
                return r.personal_info.show_profile(phone_number)

            # ── sec_inventory → show inventory/catalog menu ──
            if bid == "sec_inventory":
                return r.catalog.show_menu(phone_number)

            # ── sec_supplies → show catalog (services terminology) ──
            if bid == "sec_supplies":
                return r.catalog.show_menu(phone_number)

            # ── Personal Info buttons ──
            if bid.startswith("pi_") or bid == "set_password":
                if r.personal_info:
                    return r.personal_info.handle_button(phone_number, bid)

            # ── Settings buttons ──
            if bid.startswith("set_") or bid.startswith("set_ind_"):
                if r.settings:
                    return r.settings.handle_button(phone_number, bid)

            # ── Industry change list selections ──
            if bid.startswith("set_ind_"):
                if r.settings:
                    return r.settings.handle_button(phone_number, bid)

            # ── Business tab buttons → reports handler ──
            biz_map = {
                "biz_dashboard":  lambda: r.profile.show(phone_number),
                "biz_reports":    lambda: r.reports.show(phone_number),
                "biz_sales":      lambda: r.reports.handle_button(phone_number, "biz_sales", session),
                "biz_purchases":  lambda: r.reports.handle_button(phone_number, "biz_purchases", session),
                "biz_expenses":   lambda: r.reports.handle_button(phone_number, "biz_expenses", session),
                "biz_debts":      lambda: r.debt.show_summary(phone_number),
                "biz_docs":       lambda: r.export.show_options(phone_number),
                "biz_export":     lambda: r.export.show_options(phone_number),
                "biz_recurring":  lambda: r.recurring.show(phone_number),
                "biz_quotes":     lambda: r._show_quotes(phone_number),
            }
            handler = biz_map.get(bid)
            if handler:
                return handler()

        # ── Export buttons ──
        if bid.startswith("export_"):
            return r.export.handle_button(phone_number, bid, session)

        # ── Industry change list taps (set_ind_trading etc) ──
        if bid.startswith("set_ind_"):
            if r.settings:
                return r.settings.handle_button(phone_number, bid)

        # ── pi_bank flow — bank step confirm button ──
        if bid == "pi_bank_start_flow":
            if r.personal_info:
                return r.personal_info._bank_step_1(phone_number)

        # ── Unknown button — show home menu ──
        logger.warning(f"Unknown button: {bid}")
        return r._show_home_menu(phone_number)
