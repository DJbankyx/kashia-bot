# src/core/pin_guard.py
"""PIN guard — verifies PIN before sensitive actions.

Usage from any handler:
    from core.pin_guard import requires_pin, verify_pin, PIN_VERIFYING

    # Before a sensitive action:
    result = requires_pin(db, session_mgr, phone_number, action_id="export")
    if result:
        return result  # Bot asked for PIN — stop here

    # In state routing (when state == PIN_VERIFYING):
    if user typed their PIN:
        return verify_pin(db, session_mgr, phone_number, text, session)
"""

import hashlib
import time
import logging

from utils.whatsapp_ui import text_response

logger = logging.getLogger(__name__)

PIN_VERIFYING = "PIN_VERIFYING"

# Actions that require PIN when set
PROTECTED_ACTIONS = {
    "export", "bank_details", "reset_account", "statement",
    "export_excel", "export_csv", "export_statement",
    "pi_bank", "set_reset",
}

# How long PIN stays unlocked (seconds) — 1 hour
PIN_SESSION_TTL = 3600

# Max wrong attempts before lockout
MAX_ATTEMPTS = 3

# Lockout duration (seconds) — 15 minutes
LOCKOUT_DURATION = 900


def requires_pin(db, session_mgr, phone_number: str, action_id: str) -> list:
    """
    Check if this action needs PIN verification.
    
    Returns:
        None — no PIN required, continue with action
        list of responses — PIN prompt was shown, caller should return these
    """
    # Only enforce if action is protected
    if action_id not in PROTECTED_ACTIONS:
        return None

    # Check if user has a PIN set
    user = db.get_user(phone_number)
    if not user:
        return None
    pin_hash = user.get("pin_hash", "")
    if not pin_hash:
        return None  # No PIN set — allow action

    # Check if already verified this session (within TTL)
    pin_verified_at = user.get("pin_verified_at", 0)
    if pin_verified_at and (time.time() - int(pin_verified_at)) < PIN_SESSION_TTL:
        return None  # Still within verified window — allow

    # Check for lockout
    pin_lockout_until = int(user.get("pin_lockout_until", 0))
    if pin_lockout_until > time.time():
        remaining = int((pin_lockout_until - time.time()) / 60) + 1
        return [text_response(
            f"🔒 *Account locked*\n\n"
            f"Too many wrong PIN attempts.\n"
            f"Try again in {remaining} minutes."
        )]

    # PIN is set but not verified — ask for it
    session_mgr.save(phone_number, PIN_VERIFYING, {
        "pin_action": action_id,
        "pin_attempts": 0,
    })

    return [text_response(
        "🔒 *Enter your PIN to continue:*\n\n"
        "_Type your 4-digit PIN:_"
    )]


def verify_pin(db, session_mgr, phone_number: str, text: str, session: dict) -> list:
    """
    Verify the PIN the user just typed.
    
    Returns:
        list of responses:
        - If correct: returns a __PIN_VERIFIED__ marker with the original action_id
        - If wrong: asks again or locks out
    """
    context  = session.get("context", {})
    action_id = context.get("pin_action", "")
    attempts  = int(context.get("pin_attempts", 0))
    pin_text  = text.strip()

    if pin_text.lower() in ("cancel", "exit", "back"):
        session_mgr.reset(phone_number)
        return [text_response("👍 Cancelled.")]

    # Get stored hash
    user = db.get_user(phone_number)
    if not user:
        session_mgr.reset(phone_number)
        return [text_response("❌ Error. Please try again.")]

    stored_hash = user.get("pin_hash", "")
    entered_hash = hashlib.sha256(pin_text.encode()).hexdigest()

    if entered_hash == stored_hash:
        # ✅ Correct — mark as verified for 1 hour
        db.update_user(phone_number, {
            "pin_verified_at": int(time.time()),
            "pin_attempts": 0,
        })
        session_mgr.reset(phone_number)

        # Return marker so main.py can re-execute the original action
        return [{"type": "__PIN_VERIFIED__", "content": {"action_id": action_id}}]

    else:
        # ❌ Wrong PIN
        attempts += 1

        if attempts >= MAX_ATTEMPTS:
            # Lock out
            lockout_until = int(time.time()) + LOCKOUT_DURATION
            db.update_user(phone_number, {
                "pin_lockout_until": lockout_until,
                "pin_attempts": attempts,
            })
            session_mgr.reset(phone_number)
            return [text_response(
                f"🔒 *Too many wrong attempts!*\n\n"
                f"Account locked for 15 minutes.\n"
                f"_Try again later._"
            )]

        # Ask again
        remaining = MAX_ATTEMPTS - attempts
        session_mgr.save(phone_number, PIN_VERIFYING, {
            "pin_action": action_id,
            "pin_attempts": attempts,
        })
        return [text_response(
            f"❌ Wrong PIN. {remaining} attempt{'s' if remaining != 1 else ''} left.\n\n"
            f"_Enter your 4-digit PIN or type *cancel*:_"
        )]
