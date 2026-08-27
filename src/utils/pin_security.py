# src/utils/pin_security.py
"""PIN hashing — salted PBKDF2-HMAC-SHA256 with transparent legacy migration.

Why: PINs were previously stored as unsalted SHA-256, which is fast to brute
force and identical for identical PINs across users. This module upgrades to
PBKDF2 with a per-user random salt (stdlib only — no extra dependencies) and
verifies old SHA-256 hashes so existing users are migrated seamlessly.

Stored format (new):  pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
Stored format (old):  <64-char sha256 hex>   (still verified, then upgraded)
"""

import hashlib
import hmac
import os

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 200_000
_SALT_BYTES = 16


def hash_pin(pin: str) -> str:
    """Hash a PIN with PBKDF2-HMAC-SHA256 and a fresh random salt."""
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def _is_legacy_sha256(stored: str) -> bool:
    """Old format was a bare 64-char hex SHA-256 digest with no '$' separators."""
    return bool(stored) and "$" not in stored and len(stored) == 64


def verify_pin(pin: str, stored: str) -> tuple[bool, bool]:
    """
    Verify a PIN against a stored hash.

    Returns (is_valid, needs_upgrade):
      - is_valid: whether the PIN matches
      - needs_upgrade: True when the match was against a legacy SHA-256 hash,
        signalling the caller to re-hash and store in the new format.
    """
    if not stored or not pin:
        return False, False

    # Legacy unsalted SHA-256
    if _is_legacy_sha256(stored):
        legacy = hashlib.sha256(pin.encode("utf-8")).hexdigest()
        return hmac.compare_digest(legacy, stored), True

    # New PBKDF2 format
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$", 3)
        if algo != _ALGO:
            return False, False
        iterations = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(dk.hex(), hash_hex), False
    except (ValueError, TypeError):
        return False, False
