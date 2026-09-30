"""Open items — Stage 1 of the customer sub-accounts / open-item debt model.

See docs/CUSTOMER_SUBACCOUNTS_OPEN_ITEMS_PLAN.md.

An OPEN ITEM is simply an unpaid credit sale (or credit purchase): a transaction
row whose ``balance_owed > 0``. This module is a thin VIEW + a smarter SETTLE on
top of the EXISTING model — it does NOT introduce a new store or change how
reports read money:

  * The contact's lump ``debt_owed_to_me`` / ``debt_i_owe`` field STAYS the
    authoritative total (accounting.position sums those). We keep it reconciled.
  * Open items let the owner see WHICH sales are unpaid and apply a payment to a
    CHOSEN sale ("I pick" — e.g. rice paid, oil still owing), instead of only
    reducing one lump number.

Backward-compatible: a customer with no open-item data behaves exactly as before
(the lump field is used); WhatsApp + trading are untouched. Money is kobo-precise
(utils.money); nothing here touches P&L (settlements are already excluded via
accounting._is_debt_settlement when a repayment row is logged by the caller).
"""

import logging

from utils.money import money_round, to_money

logger = logging.getLogger(__name__)


# Which transaction types can carry an open (unpaid) balance, per debt direction.
# owed_to_me = customers owe me (unpaid SALES); i_owe = I owe suppliers (unpaid
# PURCHASES). Expenses/returns/production/etc. never carry a receivable here.
_TYPES_FOR = {
    "owed_to_me": ("sale", "income"),
    "i_owe": ("purchase",),
}


def _norm(name: str) -> str:
    return str(name or "").strip().lower()


class OpenItems:
    """Read/settle open items for a contact. Constructed with a Database."""

    def __init__(self, database):
        self.db = database

    # ── READ ────────────────────────────────────────────────────────────
    def list_open_items(self, phone_number: str, contact_name: str,
                         direction: str = "owed_to_me", limit_scan: int = 500,
                         site: str = None) -> list:
        """Return this contact's OPEN items (unpaid credit sales/purchases),
        OLDEST first (the natural pay-down order). Each item:

          {transaction_id, date, at, description, amount, balance_owed, type, site}

        An item is open when its row's ``balance_owed`` (falling back to the
        original amount for a legacy full-credit row that never stamped one) is
        > 0. ``site`` (Stage 2) is the optional sub-account label on the row
        ("Delta shop"); "" for a flat customer. Pass ``site`` to filter to ONE
        site (site="" returns only unsited items). Never raises; returns [].
        """
        want = _norm(contact_name)
        if not want:
            return []
        types = _TYPES_FOR.get(direction, _TYPES_FOR["owed_to_me"])
        try:
            txns = self.db.get_transactions(phone_number, limit=limit_scan) or []
        except Exception as e:
            logger.warning(f"list_open_items get_transactions failed: {e}")
            return []
        items = []
        for t in txns:
            if t.get("type") not in types:
                continue
            if _norm(t.get("vendor")) != want:
                continue
            # Only CREDIT/part rows carry a balance. A cash sale has none.
            pm = str(t.get("payment_method") or "").lower()
            if pm not in ("credit", "deposit"):
                # A legacy full-credit row might not have pm set to credit but
                # DID create a debt; treat it as open only if it stamped a
                # positive balance_owed (avoids counting paid cash sales).
                if not t.get("balance_owed"):
                    continue
            bal = self._row_balance(t)
            if bal <= 0:
                continue
            row_site = str(t.get("site") or "").strip()
            if site is not None and row_site != str(site).strip():
                continue   # site filter (site="" → only unsited items)
            items.append({
                "transaction_id": str(t.get("transaction_id") or ""),
                "date": str(t.get("date") or ""),
                "at": str(t.get("created_at") or t.get("at") or ""),
                "description": str(t.get("item_name") or t.get("description") or "Sale"),
                "amount": money_round(t.get("amount", 0) or 0),
                "balance_owed": money_round(bal),
                "type": str(t.get("type") or ""),
                "site": row_site,
            })
        # Oldest first (created_at, then date) — the default pay-down order.
        items.sort(key=lambda x: (x.get("at") or "", x.get("date") or ""))
        return items

    def sites_summary(self, phone_number: str, contact_name: str,
                      direction: str = "owed_to_me") -> dict:
        """Group this contact's open balance BY SITE (Stage 2). Delivers BOTH
        report views the owner asked for: per-site balances AND the customer total.
        Returns:
          {total, sites: [{site, balance, count}], has_sites}
        where `sites` is sorted biggest-balance first and unsited items appear
        under site "" (rendered as "No location" / the flat balance). Never raises.
        """
        items = self.list_open_items(phone_number, contact_name, direction)
        buckets = {}
        for it in items:
            s = it.get("site") or ""
            b = buckets.setdefault(s, {"site": s, "balance": to_money(0), "count": 0})
            b["balance"] = to_money(b["balance"]) + to_money(it["balance_owed"])
            b["count"] += 1
        sites = [{"site": b["site"], "balance": money_round(b["balance"]),
                  "count": b["count"]} for b in buckets.values()]
        sites.sort(key=lambda x: -to_money(x["balance"]))
        total = money_round(sum(to_money(s["balance"]) for s in sites))
        has_sites = any(s["site"] for s in sites)
        return {"total": total, "sites": sites, "has_sites": has_sites}

    @staticmethod
    def _row_balance(t: dict):
        """The open balance of a tx row. Prefer the stamped balance_owed; a legacy
        full-credit row that never stamped one falls back to its full amount."""
        raw = t.get("balance_owed")
        if raw in (None, ""):
            # Legacy full-credit sale: unpaid = the whole amount.
            return to_money(t.get("amount", 0) or 0)
        return to_money(raw)

    def open_total(self, phone_number: str, contact_name: str,
                   direction: str = "owed_to_me") -> float:
        """Sum of the contact's open-item balances (kobo-precise). This should
        reconcile with the contact's lump debt field; if it drifts (legacy data),
        the lump field remains authoritative for reports."""
        return money_round(sum(to_money(i["balance_owed"])
                               for i in self.list_open_items(phone_number, contact_name, direction)))

    # ── SETTLE ──────────────────────────────────────────────────────────
    def settle_open_items(self, phone_number: str, contact_name: str, amount,
                          direction: str = "owed_to_me", picked_ids=None,
                          site: str = None) -> dict:
        """Apply a payment of ``amount`` to this contact's open items and keep the
        lump debt field reconciled.

        * ``site`` (optional, Stage 2): restrict the payment to ONE site's items
          (e.g. the Delta shop only) — the Asaba balance is untouched. This is the
          site-level "I pick".
        * ``picked_ids`` (optional): settle THESE specific items first, in the
          order given ("I pick" — e.g. pay the oil before the rice). Any leftover
          then spills to the remaining open items oldest-first (within the site
          filter, if one is given).
        * If neither is given: pure oldest-first across all the contact's items.

        Reduces each affected row's ``balance_owed`` via db.update_transaction,
        and calls db.settle_debt ONCE for the total applied so the contact's lump
        field stays correct (that lump is what reports read). Returns:

          {ok, applied, remaining_payment, items:[{transaction_id, paid, balance_after}]}

        NOTE: this does NOT itself log a repayment/cash row — the CALLER (chat
        debt board, webhook, or the pay flow) still records the cash movement as a
        repayment so accounting._is_debt_settlement keeps it out of the P&L. This
        function only re-allocates the debt across items + reconciles the lump.
        Never raises.
        """
        pay = to_money(money_round(amount))
        if pay <= 0:
            return {"ok": False, "error": "amount must be greater than 0",
                    "applied": 0, "remaining_payment": 0, "items": []}
        try:
            # Site filter restricts which items this payment can touch (Stage 2).
            open_items = self.list_open_items(phone_number, contact_name, direction,
                                              site=site)
            if not open_items:
                where = f" at {site}" if site else ""
                return {"ok": False, "error": f"no open items for this contact{where}",
                        "applied": 0, "remaining_payment": money_round(pay), "items": []}

            # Order: picked first (in given order), then the rest oldest-first.
            by_id = {i["transaction_id"]: i for i in open_items}
            ordered = []
            seen = set()
            for pid in (picked_ids or []):
                it = by_id.get(str(pid))
                if it and it["transaction_id"] not in seen:
                    ordered.append(it)
                    seen.add(it["transaction_id"])
            for it in open_items:   # already oldest-first
                if it["transaction_id"] not in seen:
                    ordered.append(it)
                    seen.add(it["transaction_id"])

            remaining = pay
            touched = []
            for it in ordered:
                if remaining <= 0:
                    break
                bal = to_money(it["balance_owed"])
                if bal <= 0:
                    continue
                take = bal if bal <= remaining else remaining
                new_bal = money_round(bal - take)
                try:
                    self.db.update_transaction(phone_number, it["transaction_id"],
                                               {"balance_owed": new_bal})
                except Exception as e:
                    logger.warning(f"settle_open_items update {it['transaction_id']} failed: {e}")
                    continue
                remaining = to_money(remaining) - to_money(take)
                touched.append({
                    "transaction_id": it["transaction_id"],
                    "paid": money_round(take),
                    "balance_after": new_bal,
                })

            applied = money_round(to_money(pay) - to_money(remaining))
            # Reconcile the lump debt field ONCE for what we actually applied, so
            # reports (which read the lump) stay correct.
            if applied > 0:
                try:
                    self.db.settle_debt(phone_number, contact_name, applied, direction)
                except Exception as e:
                    logger.warning(f"settle_open_items settle_debt failed: {e}")
            return {
                "ok": applied > 0,
                "applied": applied,
                "remaining_payment": money_round(remaining),
                "items": touched,
            }
        except Exception as e:
            logger.error(f"settle_open_items error: {e}")
            return {"ok": False, "error": "could not apply the payment",
                    "applied": 0, "remaining_payment": money_round(pay), "items": []}


# ── Site LABEL (user-defined middle-layer name) ─────────────────────────────
# The owner names the sub-account layer themselves (Mall / Property / Project /
# Outlet / Branch…), per the locked design. We store their choice on the user and
# default to a neutral word so the UI always has a label.
DEFAULT_SITE_LABEL = "Location"
# A conservative cap so a bad value can't blow up a menu title.
_MAX_LABEL = 24


def get_site_label(db, phone_number: str) -> str:
    """The owner's chosen name for the sub-account layer (default 'Location')."""
    try:
        user = db.get_user(phone_number) or {}
        lbl = str(user.get("site_label") or "").strip()
        return lbl[:_MAX_LABEL] if lbl else DEFAULT_SITE_LABEL
    except Exception:
        return DEFAULT_SITE_LABEL


def set_site_label(db, phone_number: str, label: str) -> str:
    """Set the owner's sub-account layer name. Returns the stored label (trimmed);
    blank clears it back to the default. Never raises."""
    clean = str(label or "").strip()[:_MAX_LABEL]
    try:
        db.update_user_field(phone_number, "site_label", clean)
    except Exception as e:
        logger.warning(f"set_site_label failed: {e}")
    return clean or DEFAULT_SITE_LABEL
