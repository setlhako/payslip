"""Create an HR admin. Run: python -m app.scripts.create_admin   (can be run again to add another admin)"""
import getpass
import sys

from app.database import SessionLocal
from app.security.passwords import MIN_PASSWORD_LENGTH
from app.services.admins import AdminError, create_admin


def main() -> int:
    username = input("Username: ")
    password = getpass.getpass(f"Password (at least {MIN_PASSWORD_LENGTH} characters, hidden): ")
    if password != getpass.getpass("Repeat password: "):
        print("The two passwords do not match. Nothing was created.")
        return 1
    with SessionLocal() as db:
        try:
            user = create_admin(db, username, password)
        except AdminError as exc:
            print(f"Not created: {exc}")
            return 1
    print(f"Admin '{user.username}' created. You can now log in.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
