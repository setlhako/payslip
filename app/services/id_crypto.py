"""Encryption of full Omang / passport numbers (authenticated: Fernet = AES-128-CBC + HMAC-SHA256).

Rules for this module:
- The key comes from the environment (ID_ENCRYPTION_KEY) and is never logged, printed or put in an error message.
- Plain ID numbers go in and out of memory only; callers must never log them or put them in an error message.
- Any failure raises IdCryptoError with a fixed, generic message. It never contains the number or the key.

Key rotation: ID_ENCRYPTION_KEY is the key used to ENCRYPT. ID_ENCRYPTION_KEY_PREVIOUS may list older keys that are
still accepted for DECRYPTING. `python -m app.scripts.reencrypt_ids` then rewrites every stored value with the newest key.
"""
from collections.abc import Iterable
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class IdCryptoError(Exception):
    """Encryption or decryption failed. The message is fixed and safe to show or log."""


def split_keys(text: str) -> list[str]:
    return [k.strip() for k in (text or "").split(",") if k.strip()]


def key_is_valid(key: str) -> bool:
    try:
        Fernet(key.encode())
    except (ValueError, TypeError):
        return False
    return True


class IdCipher:
    def __init__(self, primary: str, previous: Iterable[str] = ()):
        try:
            self._fernets = MultiFernet([Fernet(k.encode()) for k in [primary, *previous]])
        except (ValueError, TypeError):
            raise IdCryptoError("The ID encryption key is missing or not valid.") from None

    def encrypt(self, plain: str) -> str:
        return self._fernets.encrypt(plain.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernets.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError):
            raise IdCryptoError("A stored ID could not be decrypted (wrong or missing key).") from None

    def rotate(self, token: str) -> str:
        """Re-encrypt a stored value with the newest key. Works for values made with any accepted key."""
        try:
            return self._fernets.rotate(token.encode("ascii")).decode("ascii")
        except (InvalidToken, ValueError, UnicodeError):
            raise IdCryptoError("A stored ID could not be decrypted (wrong or missing key).") from None


@lru_cache
def get_cipher() -> IdCipher:
    from app.config import get_settings

    s = get_settings()
    return IdCipher(s.id_encryption_key.get_secret_value(), split_keys(s.id_encryption_key_previous.get_secret_value()))


def encrypt_id(plain: str) -> str:
    return get_cipher().encrypt(plain)


def decrypt_id(token: str) -> str:
    return get_cipher().decrypt(token)
