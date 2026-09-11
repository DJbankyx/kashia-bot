"""Unit tests for the Mini App initData validator (M1). Pure + offline."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from services.miniapp_auth import validate_init_data, build_init_data

BOT_TOKEN = "123456:TEST-bot-token-abcdef"
USER = {"id": 9671, "first_name": "Banky", "username": "banky"}


class TestMiniAppAuth(unittest.TestCase):

    def test_valid_passes(self):
        init = build_init_data(BOT_TOKEN, USER)
        res = validate_init_data(init, BOT_TOKEN)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["user_id"], "tg:9671")
        self.assertEqual(res["user"]["username"], "banky")

    def test_tampered_hash_fails(self):
        init = build_init_data(BOT_TOKEN, USER)
        tampered = init.replace("hash=", "hash=deadbeef")  # corrupt the hash value
        res = validate_init_data(tampered, BOT_TOKEN)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "bad signature")

    def test_tampered_payload_fails(self):
        # Change the signed user id AFTER signing → signature must not match.
        init = build_init_data(BOT_TOKEN, USER)
        tampered = init.replace("9671", "9999")
        res = validate_init_data(tampered, BOT_TOKEN)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "bad signature")

    def test_wrong_token_fails(self):
        init = build_init_data(BOT_TOKEN, USER)
        res = validate_init_data(init, "999999:WRONG-token")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "bad signature")

    def test_expired_fails(self):
        old = int(time.time()) - 7200  # 2 hours ago
        init = build_init_data(BOT_TOKEN, USER, auth_date=old)
        res = validate_init_data(init, BOT_TOKEN, max_age_seconds=3600)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "expired")

    def test_expired_but_age_check_disabled_passes(self):
        old = int(time.time()) - 7200
        init = build_init_data(BOT_TOKEN, USER, auth_date=old)
        res = validate_init_data(init, BOT_TOKEN, max_age_seconds=0)
        self.assertTrue(res["ok"], res)

    def test_missing_hash_fails(self):
        res = validate_init_data("user=%7B%22id%22%3A1%7D&auth_date=1", BOT_TOKEN)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "no hash")

    def test_missing_user_fails(self):
        # Sign a payload with NO user field → valid signature but no user id.
        init = build_init_data(BOT_TOKEN, user={}, extra={"query_id": "abc"})
        # build_init_data always adds a user field; simulate absence by signing
        # only extra fields via a manual construction:
        import hashlib, hmac, json
        from urllib.parse import urlencode
        fields = {"auth_date": str(int(time.time())), "query_id": "abc"}
        dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
        sk = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(sk, dcs.encode(), hashlib.sha256).hexdigest()
        res = validate_init_data(urlencode(fields), BOT_TOKEN)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "no user id")

    def test_empty_and_missing_inputs(self):
        self.assertFalse(validate_init_data("", BOT_TOKEN)["ok"])
        self.assertFalse(validate_init_data(None, BOT_TOKEN)["ok"])
        init = build_init_data(BOT_TOKEN, USER)
        self.assertFalse(validate_init_data(init, "")["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
