"""Encryption of full Omang / passport numbers (authenticated: Fernet = AES-128-CBC + HMAC-SHA256).

Rules for this module:
- The key comes from the environment (ID_ENCRYPTION_KEY) and is never logged, printed or put in an error message.
- Plain ID numbers go in and out of memory only; callers must never log them or put them in an error message.
- Any failure raises IdCryptoError with a fixed, generic message. It never contains the number or the key.

Key rotation: ID_ENCRYPTION_KEY is the key used to ENCRYPT. ID_ENCRYPTION_KEY_PREVIOUS may list older keys that are
still accepted for DECRYPTING. `python -m app.scripts.reencrypt_ids` then rewrites every stored value with the newest key.

Blind index (`id_blind_index`): Fernet output is different every time, so "is this ID already used?" cannot be asked of
`id_number_encrypted`. The database therefore also keeps `employees.id_number_hash` = HMAC-SHA256 of the NORMALISED number
(upper-case, spaces and dashes removed, the ID type NOT included, so an Omang and a passport with the same characters
are the same ID). The HMAC key is derived from the NEWEST encryption key with HKDF (fixed label "id-blind-index"), so there
is no second secret to look after. Consequence: rotating ID_ENCRYPTION_KEY changes every hash, so `reencrypt_ids`
re-hashes in the same pass. The hash cannot be turned back into the number, but treat it as sensitive anyway: never log it.
"""
import base64
import hashlib
import hmac
import re
from collections.abc import Iterable
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

BLIND_INDEX_LABEL = b"id-blind-index"


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
        self._primary = primary
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

    def blind_index(self, plain: str) -> str:
        """The blind index of this number under THIS cipher's newest key (see `id_blind_index`)."""
        return blind_index_with(self._primary, plain)

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


# ---- blind index (uniqueness of ID numbers) --------------------------------------------------------------------

def normalise_id(plain: str) -> str:
    """The form that is hashed: upper-case, spaces and dashes removed. The ID type is deliberately not part of it."""
    return re.sub(r"[\s\-]+", "", plain or "").upper()


@lru_cache
def _blind_index_key(primary_key: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(primary_key.encode("ascii"))
    except (ValueError, UnicodeError):
        raise IdCryptoError("The ID encryption key is missing or not valid.") from None
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=BLIND_INDEX_LABEL).derive(raw)


def blind_index_with(primary_key: str, plain: str) -> str:
    """Hex HMAC-SHA256 of the normalised ID number (64 characters), keyed from `primary_key`."""
    return hmac.new(_blind_index_key(primary_key), normalise_id(plain).encode("utf-8"), hashlib.sha256).hexdigest()


def id_blind_index(plain: str) -> str:
    """The blind index under the configured ID_ENCRYPTION_KEY. Same number in = same value out, under one key."""
    from app.config import get_settings

    return blind_index_with(get_settings().id_encryption_key.get_secret_value(), plain)
