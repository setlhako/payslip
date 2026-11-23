"""Create ID_ENCRYPTION_KEY in your .env. The key is written straight into .env and is NEVER printed.

    python -m app.scripts.generate_encryption_key            first-time setup (does nothing if a valid key already exists)
    python -m app.scripts.generate_encryption_key --rotate   start a key rotation: the current key moves to
                                                             ID_ENCRYPTION_KEY_PREVIOUS and a new one is created.
                                                             Then run: python -m app.scripts.reencrypt_ids

Replacing the key without --rotate is deliberately not possible: stored ID numbers could no longer be read.
"""
import re
import sys

from cryptography.fernet import Fernet

from app.config import PROJECT_ROOT
from app.services.id_crypto import key_is_valid, split_keys

KEY, PREVIOUS = "ID_ENCRYPTION_KEY", "ID_ENCRYPTION_KEY_PREVIOUS"


def _value(lines: list[str], name: str) -> str:
    for line in lines:
        m = re.match(rf"^{name}=([^#\r\n]*)", line)
        if m:
            return m.group(1).strip().strip("\"'")
    return ""


def _set(lines: list[str], name: str, value: str) -> list[str]:
    out, done = [], False
    for line in lines:
        if re.match(rf"^{name}=", line):
            if not done:
                out.append(f"{name}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{name}={value}")
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    rotate = "--rotate" in args
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        print("No .env file found. Create it first:  Copy-Item .env.example .env")
        return 1

    lines = env_path.read_text(encoding="utf-8").splitlines()
    current = _value(lines, KEY)
    has_key = key_is_valid(current)

    if has_key and not rotate:
        print("ID_ENCRYPTION_KEY is already set. Nothing changed.")
        print("(To replace it safely, use --rotate and follow the README. Never just delete the old key.)")
        return 0
    if rotate and not has_key:
        print("There is no valid ID_ENCRYPTION_KEY to rotate. Run without --rotate to create the first one.")
        return 1

    if rotate:
        previous = split_keys(_value(lines, PREVIOUS))
        lines = _set(lines, PREVIOUS, ",".join([current, *previous]))   # newest old key first
    lines = _set(lines, KEY, Fernet.generate_key().decode())
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if rotate:
        print("A new ID_ENCRYPTION_KEY was written to .env. The old key was kept in ID_ENCRYPTION_KEY_PREVIOUS.")
        print("Next: python -m app.scripts.reencrypt_ids   (then see README for removing the old key)")
    else:
        print("ID_ENCRYPTION_KEY written to .env. Back up your .env file somewhere safe: without this key,")
        print("stored ID numbers cannot be read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
