"""Reset a user's password from the command line. Emergency path (for example the only admin is locked out).

    python -m app.scripts.reset_admin_password                 asks for the username and the new password
    python -m app.scripts.reset_admin_password --enable        also re-enables a DISABLED account
    python -m app.scripts.reset_admin_password --must-change   they must choose their own password at the next login
Ends every open session of that user and clears their lockout."""
import argparse
import getpass
import sys

from app.database import SessionLocal
from app.security.passwords import MIN_PASSWORD_LENGTH
from app.services.admins import AdminError, reset_password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset a user's password.")
    parser.add_argument("--enable", action="store_true", help="also re-enable a disabled account")
    parser.add_argument("--must-change", action="store_true", help="force a password change at the next login")
    args = parser.parse_args(argv)
    username = input("Username: ")
    password = getpass.getpass(f"New password (at least {MIN_PASSWORD_LENGTH} characters, hidden): ")
    if password != getpass.getpass("Repeat new password: "):
        print("The two passwords do not match. Nothing was changed.")
        return 1
    with SessionLocal() as db:
        try:
            user = reset_password(db, username, password, must_change=args.must_change, enable=args.enable)
        except AdminError as exc:
            print(f"Not changed: {exc}")
            return 1
    print(f"Password for '{user.username}' changed. Any existing login sessions are now invalid."
          + (" The account is enabled." if args.enable else "") + (" They must choose a new password at the next login." if args.must_change else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
