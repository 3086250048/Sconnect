from __future__ import annotations

import base64
import hashlib
import hmac
import unittest

from app.credentials import create_turn_credential, hash_api_key


class CredentialTests(unittest.TestCase):
    def test_turn_rest_credential_matches_coturn_algorithm(self) -> None:
        credential = create_turn_credential("secret-value", "acct_test", 3600, now=1_700_000_000)
        username = "1700003600:acct_test"
        expected = base64.b64encode(hmac.new(b"secret-value", username.encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(credential.username, username)
        self.assertEqual(credential.password, expected)
        self.assertEqual(credential.expires_at, 1_700_003_600)

    def test_ttl_is_bounded(self) -> None:
        with self.assertRaises(ValueError):
            create_turn_credential("secret", "acct", 30)
        with self.assertRaises(ValueError):
            create_turn_credential("secret", "acct", 90000)

    def test_api_key_hash_is_stable(self) -> None:
        self.assertEqual(hash_api_key("abc"), hash_api_key("abc"))
        self.assertNotEqual(hash_api_key("abc"), hash_api_key("abd"))


if __name__ == "__main__":
    unittest.main()

