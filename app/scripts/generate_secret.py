"""Create SESSION_SECRET in your .env. Run: python -m app.scripts.generate_secret [--force]

The secret is written straight into .env and is never printed.
"""
import re
import secrets
import sys

from app.config import PROJECT_ROOT


def main() -> int:
    force = "--force" in sys.argv
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        print("No .env file found. Create it first:  Copy-Item .env.example .env")
        return 1

    lines = env_path.read_text(encoding="utf-8").splitlines()
    current = next((m.group(1) for l in lines if (m := re.match(r"^SESSION_SECRET=([^#\s]*)", l))), "")
    if len(current.strip("\"'")) >= 32 and not force:
        print("SESSION_SECRET is already set. Nothing changed.")
        print("(Use --force to replace it. That logs everyone out.)")
        return 0

    new_line = f"SESSION_SECRET={secrets.token_urlsafe(48)}"
    out, done = [], False
    for line in lines:
        if re.match(r"^(SESSION_SECRET|SECRET_KEY)=", line):  # SECRET_KEY = the old name
            if not done:
                out.append(new_line)
                done = True
            continue
        out.append(line)
    if not done:
        out.append(new_line)
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("SESSION_SECRET written to .env.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
