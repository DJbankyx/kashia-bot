# src/utils/config.py
"""Configuration - loads secrets from environment variables or SSM"""

import os
import boto3

# Cache SSM values so we don't fetch them every Lambda call
_cache = {}


def get_parameter(name):
    """Get a parameter from SSM Parameter Store (with caching)"""
    if name in _cache:
        return _cache[name]

    ssm = boto3.client('ssm')
    response = ssm.get_parameter(Name=name, WithDecryption=True)
    value = response['Parameter']['Value']
    _cache[name] = value
    return value


def get_whatsapp_token():
    return get_parameter('/kashia/whatsapp-token')


def get_phone_number_id():
    return get_parameter('/kashia/whatsapp-phone-number-id')


def get_telegram_bot_token():
    """Telegram Bot API token from @BotFather (SSM: /kashia/telegram-bot-token)."""
    return get_parameter('/kashia/telegram-bot-token')


def get_verify_token():
    return get_parameter('/kashia/whatsapp-verify-token')


def get_openai_key():
    return get_parameter('/kashia/openai-api-key')

def get_app_secret():
    return get_parameter('/kashia/meta-app-secret')


def get_paystack_secret():
    """Paystack secret key for payment processing"""
    return get_parameter('/kashia/paystack-secret-key')


def get_admin_chat_id():
    """Admin Telegram chat id for feedback/support forwarding
    (SSM: /kashia/admin-chat-id). OPTIONAL — returns None when unset so callers
    fall back to forwarding to the submitting owner's own chat. Never raises."""
    try:
        return get_parameter('/kashia/admin-chat-id')
    except Exception:
        return None


def get_admin_chat_ids():
    """Set of admin ids (SSM: /kashia/admin-chat-ids, comma-separated) for admin
    tools (broadcast / reply / mini-app admin inbox). Folds in the single
    /kashia/admin-chat-id so the existing feedback setup keeps working. Ids are
    stored bare (no "tg:" prefix) by convention but callers normalise both forms.
    Returns a set (possibly empty). Never raises."""
    ids = set()
    try:
        raw = get_parameter('/kashia/admin-chat-ids')
    except Exception:
        raw = None
    if raw:
        ids.update(x.strip() for x in str(raw).split(',') if x.strip())
    try:
        single = get_admin_chat_id()
        if single:
            ids.add(str(single).strip())
    except Exception:
        pass
    return ids
