"""Admin CLI: docker compose exec api python -m noc.api.manage <command>

  set-password <username> [admin|viewer]   create/update a user (password read from stdin or prompt)
  list-users
"""
import asyncio
import getpass
import sys

from ..db import connect
from .auth import hash_password


async def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("set-password", "list-users"):
        print(__doc__)
        return 2
    pool = await connect(max_size=1)
    try:
        if argv[0] == "list-users":
            for r in await pool.fetch("""SELECT username, is_owner, disabled, created_at, last_login,
                (SELECT count(*) FROM user_site_roles g WHERE g.username=u.username) AS sites FROM users u ORDER BY username"""):
                print(f"{r['username']:<20} {'owner' if r['is_owner'] else 'site-scoped':<12} sites={r['sites']} disabled={r['disabled']} last login {r['last_login'] or '-'}")
            return 0
        if len(argv) < 2:
            print(__doc__)
            return 2
        user, role = argv[1], (argv[2] if len(argv) > 2 else "admin")
        if role not in ("admin", "viewer"):
            print("role must be admin or viewer")
            return 2
        pw = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("New password: ")
        if len(pw) < 12 or len(pw.encode()) > 72:
            print("password must be at least 12 characters and at most 72 UTF-8 bytes")
            return 2
        await pool.execute(
                """INSERT INTO users (username, password_hash, role, is_owner)
                    VALUES ($1,$2,'viewer', $3='admin' AND NOT EXISTS(SELECT 1 FROM users))
                    ON CONFLICT (username) DO UPDATE SET password_hash=EXCLUDED.password_hash""", user, hash_password(pw), role)
        await pool.execute("DELETE FROM sessions WHERE username=$1", user)
        print(f"user {user} saved; existing site permissions preserved; existing sessions revoked")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
