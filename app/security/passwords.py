"""Password hashing (Argon2) and rules for usernames / passwords. Plain passwords are never stored or logged."""
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()  # argon2id with sensible defaults

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
_COMMON = {
    "password1234", "password12345", "123456789012", "qwertyuiop12", "administrator",
    "letmein12345", "welcome12345", "iloveyou1234", "payslip12345", "changeme1234",
}

# Hash of a random throw-away password. Checked when the username does not exist,
# so that "unknown user" takes about as long as "wrong password".
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """True only if `password` matches. Always does a full Argon2 check, even for unknown users."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def normalize_username(username: str) -> str:
    return username.strip().lower()


_USERNAME_RE = re.compile(r"^[a-z0-9._@-]{3,150}$")


def username_problem(username: str) -> str | None:
    """Returns a message if the (normalised) username is not acceptable, else None."""
    if not _USERNAME_RE.match(username):
        return "Username must be 3-150 characters: letters, numbers and . _ @ - only."
    return None


def password_problem(password: str, username: str = "") -> str | None:
    """Returns a message explaining why the password is too weak, or None if it is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters long."
    if len(set(password)) < 5:
        return "Password is too repetitive. Use a mix of different characters."
    lowered = password.lower()
    if lowered in _COMMON:
        return "That password is far too common."
    if username and username.lower() in lowered:
        return "Password must not contain the username."
    return None
