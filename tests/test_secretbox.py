"""Secrets at rest: AI keys and Google tokens are only ever stored encrypted."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import secretbox  # noqa: E402

A, B = b"a" * 32, b"b" * 32


class SecretboxTest(unittest.TestCase):
    def test_round_trip_and_no_plain_text(self):
        ring = secretbox.Keyring([A])
        token = ring.encrypt("sk-live-1234567890", "user:2:ai_config")
        self.assertTrue(token.startswith("enc1:"))
        self.assertNotIn("1234567890", token)
        self.assertEqual(ring.decrypt(token, "user:2:ai_config"), "sk-live-1234567890")
        self.assertNotEqual(token, ring.encrypt("sk-live-1234567890", "user:2:ai_config"))  # fresh nonce each time

    def test_tampering_or_moving_it_to_another_user_fails(self):
        ring = secretbox.Keyring([A])
        token = ring.encrypt('{"api_key": "x"}', "user:2:ai_config")
        with self.assertRaises(secretbox.SecretError):
            ring.decrypt(token, "user:3:ai_config")  # copied into someone else's row
        flipped = token[:-6] + ("A" if token[-6] != "A" else "B") + token[-5:]
        with self.assertRaises(secretbox.SecretError):
            ring.decrypt(flipped, "user:2:ai_config")
        with self.assertRaises(secretbox.SecretError):
            secretbox.Keyring([B]).decrypt(token, "user:2:ai_config")  # wrong encryption key

    def test_key_rotation(self):
        old = secretbox.Keyring([A]).encrypt("secret", "c")
        ring = secretbox.Keyring([B, A])  # new key first, old one still accepted
        self.assertEqual(ring.decrypt(old, "c"), "secret")
        self.assertTrue(ring.needs_reencrypting(old, "c"))
        self.assertFalse(ring.needs_reencrypting(ring.encrypt("secret", "c"), "c"))
        self.assertTrue(ring.needs_reencrypting("plain text from before", "c"))
        self.assertFalse(secretbox.Keyring([B]).needs_reencrypting(old, "c"))  # can't open: leave it alone


if __name__ == "__main__":
    unittest.main()
