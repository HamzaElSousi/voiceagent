"""Encryption at rest for workspace provider keys (Fernet: AES-128-CBC + HMAC-SHA256).

The key is the instance ENCRYPTION_KEY. Losing it makes every stored provider key unrecoverable
(workspaces would have to re-enter them), so `voiceagent init-instance` tells the operator to back
it up next to the database backups.
"""

from __future__ import annotations

from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


class EncryptionKeyMissing(RuntimeError):
    pass


class SecretBox:
    def __init__(self, key: Optional[str]):
        if not key:
            raise EncryptionKeyMissing(
                "ENCRYPTION_KEY is not set. Generate one with `voiceagent init-instance`."
            )
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise EncryptionKeyMissing(f"ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("could not decrypt (wrong ENCRYPTION_KEY?)") from exc


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


def last4(value: str) -> str:
    return value[-4:] if len(value) > 8 else ""


def mask(value_last4: str) -> str:
    return f"****{value_last4}" if value_last4 else "****"
