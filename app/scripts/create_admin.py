"""Create a user from the command line (an ADMIN unless you say otherwise). Emergency path: day-to-day, admins use the Users page.

    python -m app.scripts.create_admin                      asks for username and password, makes an admin
    python -m app.scripts.create_admin --role hr            makes an HR user
    python -m app.scripts.create_admin --must-change        the person must choose their own password at first login
    python -m app.scripts.create_admin --name "Thabo Molefe"
(can be run again to add another user)"""
import argparse
import getpass
import sys

from app.database import SessionLocal
from app.security.passwords import MIN_PASSWORD_LENGTH
from app.security.permissions import ROLES
from app.services.admins import AdminError, create_admin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a user (an admin by default).")
    parser.add_argument("--role", choices=ROLES, default="admin")
    parser.add_argument("--name", default="", help="full name (optional)")
    parser.add_argument("--must-change", action="store_true", help="force a password change at the first login")
    args = parser.parse_args(argv)
    username = input("Username: ")
    password = getpass.getpass(f"Password (at least {MIN_PASSWORD_LENGTH} characters, hidden): ")
    if password != getpass.getpass("Repeat password: "):
        print("The two passwords do not match. Nothing was created.")
        return 1
    with SessionLocal() as db:
        try:
            user = create_admin(db, username, password, role=args.role, full_name=args.name, must_change=args.must_change)
        except AdminError as exc:
            print(f"Not created: {exc}")
            return 1
    print(f"{args.role.capitalize()} '{user.username}' created. You can now log in."
          + (" They will be asked to choose a new password first." if args.must_change else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
