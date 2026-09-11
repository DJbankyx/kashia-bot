"""Subscription Reminder + Auto-Downgrade Lambda (build #S2).

Triggered daily by EventBridge. For every PAID user (basic/pro) that has a
subscription window on record, it:
  • sends a renewal nudge at T-7 / T-3 / T-1 days before expiry (with a Renew
    button / "type UPGRADE" CTA),
  • sends a "payment overdue" nudge during the 3-day grace window,
  • auto-downgrades to Free once past the grace window and tells the user.

Grandfathered users (paid but NO subscription_ends — from before the lifecycle
feature) are SKIPPED entirely: they're never auto-downgraded until they renew.

Delivery reuses the shared cross-platform path (resolve_client → send_text),
exactly like scheduled_reports.py, so Telegram users get it on Telegram and
WhatsApp users on WhatsApp. A send-once guard (last_renewal_nudge = "YYYY-MM-DD:
<bucket>") stops the same bucket being re-sent on the next daily run.

No forked business logic: the state machine + downgrade live in
TierManager.subscription_status / downgrade_user (S1).
"""

import logging
from datetime import datetime

from services.database import Database
from services.tier_manager import TierManager
from services.whatsapp_client import WhatsAppClient
from services.messaging_client import resolve_client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Days-before-expiry that trigger a renewal nudge.
NUDGE_DAYS = {7, 3, 1}


def lambda_handler(event, context):
    """Scan paid users, send renewal/expiry alerts, auto-downgrade lapsed ones.
    Idempotent — safe to run daily (send-once guard + state re-derived each run)."""
    try:
        db = Database()
        tier = TierManager(database=db)
        whatsapp = WhatsAppClient()

        nudged = downgraded = skipped = errors = 0

        for user in _iter_paid_users(db):
            user_id = user.get("phone_number", "")
            if not user_id:
                continue
            try:
                result = _process_user(db, tier, whatsapp, user_id, user)
                if result == "nudged":
                    nudged += 1
                elif result == "downgraded":
                    downgraded += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(f"subscription_reminder: error for {user_id}: {e}")
                errors += 1
                continue

        logger.info(f"Subscription reminder complete: nudged={nudged} "
                    f"downgraded={downgraded} skipped={skipped} errors={errors}")
        return {"status": "ok", "nudged": nudged, "downgraded": downgraded,
                "skipped": skipped, "errors": errors}
    except Exception as e:
        logger.error(f"subscription_reminder Lambda error: {e}")
        return {"status": "error", "message": str(e)}


def _process_user(db, tier, whatsapp, user_id, user):
    """Handle one user. Returns 'nudged' | 'downgraded' | 'skip'."""
    status = tier.subscription_status(user_id)
    state = status.get("state")
    days_left = status.get("days_left")

    # Only paid, dated subscriptions matter. free + grandfathered are skipped
    # (grandfathered = existing paid user without an end date → never expire).
    if state in ("free", "grandfathered"):
        return "skip"

    # ── Expired past the grace window → auto-downgrade + notify (once) ──
    if state == "expired":
        # Guard so we only downgrade/notify once (the downgrade itself flips tier
        # to free, so the next run sees 'free' and skips — but guard anyway).
        if not _already_sent(user, "expired"):
            tier.downgrade_user(user_id, reason="subscription_expired")
            _send(db, whatsapp, user_id,
                  _expired_msg(status), buttons=_renew_buttons())
            _stamp(db, user_id, "expired")
        return "downgraded"

    # ── In the 3-day grace window → "payment overdue" nudge (once) ──
    if state == "grace":
        if not _already_sent(user, "grace"):
            _send(db, whatsapp, user_id,
                  _grace_msg(status), buttons=_renew_buttons())
            _stamp(db, user_id, "grace")
            return "nudged"
        return "skip"

    # ── Active → renewal nudge at T-7 / T-3 / T-1 (once per bucket) ──
    if state == "active" and days_left in NUDGE_DAYS:
        bucket = f"T-{days_left}"
        if not _already_sent(user, bucket):
            _send(db, whatsapp, user_id,
                  _renewal_msg(status, days_left), buttons=_renew_buttons())
            _stamp(db, user_id, bucket)
            return "nudged"
    return "skip"


# ── send-once guard ──────────────────────────────────────────────────────────
def _already_sent(user, bucket):
    """True if today's run already sent this bucket to this user."""
    today = datetime.now().strftime("%Y-%m-%d")
    return user.get("last_renewal_nudge") == f"{today}:{bucket}"


def _stamp(db, user_id, bucket):
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        db.update_user(user_id, {"last_renewal_nudge": f"{today}:{bucket}"})
    except Exception as e:
        logger.warning(f"subscription_reminder: stamp failed for {user_id}: {e}")


# ── delivery (mirrors scheduled_reports.py) ───────────────────────────────────
def _send(db, whatsapp, user_id, text, buttons=None):
    client, recipient = resolve_client(user_id, whatsapp_fallback=whatsapp)
    if client is None:
        client, recipient = whatsapp, user_id
    try:
        if buttons and hasattr(client, "send_buttons"):
            client.send_buttons(recipient, text, buttons)
        else:
            client.send_text(recipient, text)
    except Exception as e:
        # Never let a delivery failure abort the whole run.
        logger.warning(f"subscription_reminder: send failed for {user_id}: {e}")
        try:
            client.send_text(recipient, text)
        except Exception:
            pass


def _renew_buttons():
    # Routes into the existing upgrade picker (set_upgrade is handled by the
    # settings button dispatcher; "UPGRADE" also works as typed text).
    return [{"id": "set_upgrade", "title": "💳 Renew now"}]


# ── message copy ──────────────────────────────────────────────────────────────
def _plan_name(status):
    return str(status.get("tier", "")).capitalize() or "your plan"


def _renewal_msg(status, days_left):
    when = "tomorrow" if days_left == 1 else f"in {days_left} days"
    return (
        f"🔔 *{_plan_name(status)} renews {when}.*\n\n"
        f"Keep your unlimited features active — tap *Renew now* or type *UPGRADE* "
        f"to pay.\n\n"
        f"_Ignore this if you'll let it lapse; you'll drop to Free._"
    )


def _grace_msg(status):
    return (
        f"⚠️ *{_plan_name(status)} has expired.*\n\n"
        f"You have a short grace period before your account drops to *Free* "
        f"(limits will apply). Renew now to keep everything active — tap *Renew "
        f"now* or type *UPGRADE*.\n\n"
        f"_Your data is safe either way._"
    )


def _expired_msg(status):
    return (
        f"⛔ *Your subscription has ended — you're now on Free.*\n\n"
        f"Free limits now apply (e.g. monthly transaction cap). *Nothing was "
        f"deleted* — all your records are safe.\n\n"
        f"Come back anytime — tap *Renew now* or type *UPGRADE*."
    )


# ── user scan (paid only, cheap projection) ───────────────────────────────────
def _iter_paid_users(db):
    """Yield full user items for BASIC/PRO users only, handling pagination.
    Filters at the DynamoDB level so free users aren't fetched."""
    try:
        from boto3.dynamodb.conditions import Attr
        kwargs = {
            # Only paid tiers; free/unset are irrelevant to the lifecycle.
            "FilterExpression": Attr("tier").is_in(["basic", "pro"]),
        }
        response = db.users.scan(**kwargs)
        for item in response.get("Items", []):
            yield item
        while "LastEvaluatedKey" in response:
            response = db.users.scan(
                ExclusiveStartKey=response["LastEvaluatedKey"], **kwargs)
            for item in response.get("Items", []):
                yield item
    except Exception as e:
        logger.error(f"subscription_reminder: user scan failed: {e}")
        return
