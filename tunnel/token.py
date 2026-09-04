"""Get (or create, or rotate) the server's AUTH_TOKEN.

    python tunnel/token.py            # print the token, creating one if absent
    python tunnel/token.py --rotate   # replace it with a fresh one
    python tunnel/token.py --path     # print where it is stored

One helper rather than the same logic written twice in PowerShell and bash, so
the two start scripts cannot drift apart on something security-relevant.

**The token is stable across restarts, and rotated only on request.** A fresh
token every launch would invalidate every bookmark, every phone tab and any
firmware holding it -- friction that pushes toward not setting a token at all,
and an unauthenticated server behind a public tunnel is the thing this exists to
prevent.

**Rotation is not a defence against guessing.** `secrets.token_urlsafe(32)` is
32 bytes -- 256 bits -- from the OS CSPRNG. There is no feasible search of that
space at any rate, from any number of machines, for any length of time; the
number of candidates exceeds the atom count of the observable universe by many
orders of magnitude. Rotating helps with one thing only: bounding the damage
from a token that has actually *leaked* (a screenshot, a shared terminal, a chat
log). Rotate then, not on a schedule.

**Why it lives outside the repo.** `~/.emtext/auth_token`, not the project
directory. A secret inside a git working tree is one `git add -A` away from being
published, and .gitignore only protects you until someone uses `-f` or copies the
folder. Keeping it out of the tree removes that class of accident entirely.
"""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from pathlib import Path

TOKEN_DIR = Path(os.path.expanduser("~")) / ".emtext"
TOKEN_FILE = TOKEN_DIR / "auth_token"
TOKEN_BYTES = 32   # -> 43 url-safe chars


def _write(token: str) -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        # Owner read/write only. A no-op on Windows in practice, but correct on
        # POSIX and harmless to attempt either way.
        TOKEN_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def get_token(rotate: bool = False) -> str:
    if rotate or not TOKEN_FILE.exists():
        token = secrets.token_urlsafe(TOKEN_BYTES)
        _write(token)
        return token
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        # An empty file would silently start the server unauthenticated, which
        # is exactly the failure this script exists to prevent.
        token = secrets.token_urlsafe(TOKEN_BYTES)
        _write(token)
    return token


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rotate", action="store_true",
                    help="generate a new token, invalidating the old one")
    ap.add_argument("--path", action="store_true", help="print the file path only")
    args = ap.parse_args()

    if args.path:
        print(TOKEN_FILE)
        return

    existed = TOKEN_FILE.exists()
    token = get_token(rotate=args.rotate)

    # Status goes to stderr so `TOKEN=$(python tunnel/token.py)` captures only
    # the token itself.
    if args.rotate:
        print(f"rotated -- old token is now invalid ({TOKEN_FILE})", file=sys.stderr)
    elif not existed:
        print(f"created new token at {TOKEN_FILE}", file=sys.stderr)
    print(token)


if __name__ == "__main__":
    main()
