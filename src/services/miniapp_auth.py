# src/services/miniapp_auth.py
"""Telegram Mini App auth — validate the signed `initData` a WebApp sends.

M1 of the Mini App (see docs/TG_MINIAPP_PLAN.md). PURE + OFFLINE: no network, no
AWS, no DB — just crypto over the initData string. This is the security gate for
the whole Mini App, so it lives alone and is unit-tested in isolation.

How Telegram signs initData (https://core.telegram.org/bots/webapps#validating-
data-received-via-the-mini-app):
  1. initData is a URL-encoded query string: user=...&auth_date=...&hash=...&...
  2. Take every field EXCEPT `hash`, format each as "key=value", sort them
     alphabetically by key, and join with "\n" → the data_check_string.
  3. secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)   # note arg order
  4. computed = HMAC_SHA256(key=secret_key, msg=data_check_string), hex.
  5. Valid iff computed == the provided `hash` (constant-time compare).
  6. Additionally reject if `auth_date` is older than max_age_seconds (replay).

Returns a plain dict; never raises to the caller.
"""

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)

# 24h. The HMAC signature is the real authenticity gate; auth_date is only a
# replay guard. 1h was too aggressive — reopening the Mini App later in the day
# (stale cached launch/initData) failed auth → the app stuck on "Loading…".
# 24h is the common, practical window for a day-to-day Mini App.
DEFAULT_MAX_AGE_SECONDS = 86400


def validate_init_data(init_data: str, bot_token: str,
                       max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
                       now: int = None) -> dict:
    """Validate a Telegram WebApp initData string.

    Args:
        init_data: the raw initData query string from Telegram.WebApp.initData.
        bot_token: the bot token (from SSM) used as the signing key material.
        max_age_seconds: reject if auth_date is older than this (replay guard).
                         Pass 0 to disable the age check.
        now: unix time override (for tests). Defaults to time.time().

    Returns:
        {"ok": True, "user_id": "tg:<id>", "user": {...}, "auth_date": int}
        on success, or {"ok": False, "error": "<reason>"} on any failure.
        Never raises.
    """
    try:
        if not init_data or not isinstance(init_data, str):
            return {"ok": False, "error": "missing init_data"}
        if not bot_token:
            return {"ok": False, "error": "missing bot_token"}

        # Parse WITHOUT dropping blanks and WITHOUT extra decoding surprises.
        # keep_blank_values so an empty field still participates in the check.
        pairs = parse_qsl(init_data, keep_blank_values=True)
        if not pairs:
            return {"ok": False, "error": "unparseable init_data"}

        data = {}
        for k, v in pairs:
            # A malformed string could repeat a key; last-wins mirrors Telegram.
            data[k] = v

        provided_hash = data.pop("hash", None)
        if not provided_hash:
            return {"ok": False, "error": "no hash"}

        # data_check_string: "key=value" per remaining field, sorted by key,
        # joined by newline. Values are the RAW (still URL-decoded by parse_qsl)
        # strings exactly as Telegram signed them.
        data_check_string = "\n".join(
            f"{k}={data[k]}" for k in sorted(data.keys())
        )

        secret_key = hmac.new(
            b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
        ).digest()
        computed = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(computed, provided_hash):
            return {"ok": False, "error": "bad signature"}

        # Signature is valid. Now enforce freshness.
        auth_date = 0
        try:
            auth_date = int(data.get("auth_date", "0"))
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad auth_date"}

        if max_age_seconds and auth_date > 0:
            current = int(now if now is not None else time.time())
            age = current - auth_date
            # Reject stale sessions. A small negative age (clock skew) is fine.
            if age > max_age_seconds:
                return {"ok": False, "error": "expired"}

        # Extract the user id → namespaced tg:<chat_id>.
        user = {}
        raw_user = data.get("user")
        if raw_user:
            try:
                user = json.loads(raw_user)
            except (ValueError, TypeError):
                return {"ok": False, "error": "bad user json"}
        uid = user.get("id") if isinstance(user, dict) else None
        if not uid:
            return {"ok": False, "error": "no user id"}

        return {
            "ok": True,
            "user_id": f"tg:{uid}",
            "user": user,
            "auth_date": auth_date,
        }

    except Exception as e:  # never raise to the caller
        logger.error(f"validate_init_data error: {e}")
        return {"ok": False, "error": "validation error"}


def build_init_data(bot_token: str, user: dict, auth_date: int = None,
                    extra: dict = None) -> str:
    """Build a correctly-SIGNED initData string. Intended for TESTS and local
    tooling ONLY (Telegram signs real initData). Mirrors the validation algorithm
    so tests can produce a valid string without hardcoding a hash."""
    import time as _t
    fields = {}
    if extra:
        fields.update({k: str(v) for k, v in extra.items()})
    fields["user"] = json.dumps(user, separators=(",", ":"))
    fields["auth_date"] = str(int(auth_date if auth_date is not None else _t.time()))

    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields.keys()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    h = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    from urllib.parse import urlencode
    fields["hash"] = h
    return urlencode(fields)
