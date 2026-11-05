"""Create or promote an admin account from the command line (works against the database in DATABASE_URL).

    python -m app.admin_cli you@example.com

You'll be asked for a password (not shown, not saved anywhere except as an argon2 hash).
For an existing account, press Enter to keep its current password.
"""
import getpass
import sys

from . import auth, db


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    email = sys.argv[1]
    db.init()
    print(f"Database: {db.URL.split('@')[-1]}")
    pw = getpass.getpass("Password (Enter = keep existing): ")
    if pw and pw != getpass.getpass("Repeat password: "):
        print("Passwords don't match.")
        sys.exit(1)
    try:
        print(auth.ensure_admin(email, pw))
    except auth.AuthError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
