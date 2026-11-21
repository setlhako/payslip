"""Reset an admin's password. Run: python -m app.scripts.reset_admin_password"""
import getpass
import sys

from app.database import SessionLocal
from app.security.passwords import MIN_PASSWORD_LENGTH
from app.services.admins import AdminError, reset_password


def main() -> int:
    username = input("Username: ")
    password = getpass.getpass(f"New password (at least {MIN_PASSWORD_LENGTH} characters, hidden): ")
    if password != getpass.getpass("Repeat new password: "):
        print("The two passwords do not match. Nothing was changed.")
        return 1
    with SessionLocal() as db:
        try:
            user = reset_password(db, username, password)
        except AdminError as exc:
            print(f"Not changed: {exc}")
            return 1
    print(f"Password for '{user.username}' changed. Any existing login sessions are now invalid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
