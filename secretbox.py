"""Encrypt small secrets (AI API keys, Google tokens) before they're written to the database.

The database only ever holds "enc1:…" ciphertext; the encryption key lives outside it, in the
server's environment (ENCRYPTION_KEY) or, locally, in data/encryption.key. So someone browsing
the database, or holding a copy of it, can't read anyone's keys.

Standard library only (no AES there), built the usual way from standard parts:
  keystream  SHAKE-256(enc_key ‖ random 16-byte nonce), XORed with the secret
  integrity  HMAC-SHA256(mac_key, context ‖ nonce ‖ ciphertext), checked before decrypting
The context (whose secret, which setting) is bound into the tag, so a ciphertext copied into
another person's row won't decrypt there.
"""
import base64
import hashlib
import hmac
import secrets

PREFIX = "enc1:"
NONCE = 16
TAG = 32


class SecretError(Exception):
    pass


def _subkeys(master):
    return (hmac.new(master, b"applystack secretbox enc", hashlib.sha256).digest(),
            hmac.new(master, b"applystack secretbox mac", hashlib.sha256).digest())


def _tag(mac_key, context, nonce, body):
    return hmac.new(mac_key, context.encode() + b"\0" + nonce + body, hashlib.sha256).digest()


def _xor(enc_key, nonce, data):
    stream = hashlib.shake_256(enc_key + nonce).digest(len(data))
    return bytes(a ^ b for a, b in zip(data, stream))


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


class Keyring:
    """Encrypts with the first master key; decrypts with any of them (so keys can be rotated)."""

    def __init__(self, masters):
        if not masters:
            raise ValueError("Keyring needs at least one master key")
        self.keys = [_subkeys(m) for m in masters]

    def encrypt(self, plaintext, context):
        enc_key, mac_key = self.keys[0]
        nonce = secrets.token_bytes(NONCE)
        body = _xor(enc_key, nonce, plaintext.encode())
        return PREFIX + base64.urlsafe_b64encode(nonce + body + _tag(mac_key, context, nonce, body)).decode()

    def _open(self, token, context):
        """(plaintext, index of the master key that opened it)."""
        if not is_encrypted(token):
            raise SecretError("Not an encrypted value")
        try:
            raw = base64.urlsafe_b64decode(token[len(PREFIX):].encode())
        except ValueError:
            raise SecretError("Damaged encrypted value") from None
        if len(raw) < NONCE + TAG:
            raise SecretError("Damaged encrypted value")
        nonce, body, tag = raw[:NONCE], raw[NONCE:-TAG], raw[-TAG:]
        for i, (enc_key, mac_key) in enumerate(self.keys):
            if hmac.compare_digest(tag, _tag(mac_key, context, nonce, body)):
                return _xor(enc_key, nonce, body).decode(), i
        raise SecretError("Can't decrypt: wrong encryption key, or the value was changed")

    def decrypt(self, token, context):
        return self._open(token, context)[0]

    def needs_reencrypting(self, value, context):
        """True for plaintext, or for ciphertext made with an older key that we can still open."""
        if not is_encrypted(value):
            return True
        try:
            return self._open(value, context)[1] != 0
        except SecretError:
            return False  # can't open it: leave it alone rather than destroy it
