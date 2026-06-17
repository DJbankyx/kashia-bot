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


def get_verify_token():
    return get_parameter('/kashia/whatsapp-verify-token')


def get_openai_key():
    return get_parameter('/kashia/openai-api-key')

def get_app_secret():
    return get_parameter('/kashia/meta-app-secret')
