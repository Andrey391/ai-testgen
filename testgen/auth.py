"""Login to the studio itself.

Users live in secrets/users.json (PBKDF2 hashes). The browser gets an
HMAC-signed, HttpOnly session cookie. On first start with no users an
`admin` account is created and its password printed to the console.

Manage users:  python -m testgen.auth adduser <name>
               python -m testgen.auth deluser <name>
               python -m testgen.auth list
Set TESTGEN_AUTH=off to disable login (single-user local use).
Anyone who can open the studio may register via the login screen; set
TESTGEN_SIGNUP=off to allow only accounts created from the CLI.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time

from .vault import SECRETS

_OFF = ("off", "0", "false", "no")
ENABLED = os.environ.get("TESTGEN_AUTH", "on").lower() not in _OFF
SIGNUP = ENABLED and os.environ.get("TESTGEN_SIGNUP", "on").lower() not in _OFF
# Admins may set commands and environment of MCP connections (that is code
# execution on the server). Comma-separated usernames.
ADMINS = {u.strip() for u in os.environ.get("TESTGEN_ADMINS", "admin").split(",") if u.strip()}
COOKIE = "tg_session"
SESSION_TTL = 7 * 24 * 3600
ANONYMOUS = "local"

_USERS = SECRETS / "users.json"
_KEY = SECRETS / "session.key"
_ITERATIONS = 200_000
# Usernames key per-user files in secrets/ (vault._path), so keep them to the
# characters it leaves as is: otherwise "a b" and "a_b" would share a file.
_USERNAME = re.compile(r"[\w@.-]{1,64}")
MIN_PASSWORD = 8


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS).hex()


def _users() -> dict:
    return json.loads(_USERS.read_text("utf-8")) if _USERS.exists() else {}


def _write_users(users: dict) -> None:
    SECRETS.mkdir(parents=True, exist_ok=True)
    _USERS.write_text(json.dumps(users, indent=2), "utf-8")


def set_password(username: str, password: str) -> None:
    users = _users()
    salt = secrets.token_hex(16)
    users[username] = {"salt": salt, "hash": _hash(password, salt)}
    _write_users(users)


def register(username: str, password: str) -> None:
    """Self-registration from the login screen; ValueError explains a refusal."""
    if not _USERNAME.fullmatch(username):
        raise ValueError("Логин: до 64 символов, буквы, цифры и . _ @ -")
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Пароль должен быть не короче {MIN_PASSWORD} символов")
    if username in _users():
        raise ValueError("Такой пользователь уже есть")
    set_password(username, password)


def delete_user(username: str) -> bool:
    users = _users()
    if users.pop(username, None) is None:
        return False
    _write_users(users)
    return True


def verify(username: str, password: str) -> bool:
    u = _users().get(username)
    if not u:
        _hash(password, "00" * 16)   # same timing whether or not the user exists
        return False
    return hmac.compare_digest(u["hash"], _hash(password, u["salt"]))


def ensure_admin() -> None:
    """First start: create `admin` with a random password so the studio is never open."""
    if ENABLED and not _users():
        password = secrets.token_urlsafe(12)
        set_password("admin", password)
        print(f"Created studio user 'admin' with password: {password}\n"
              f"(change it: python -m testgen.auth adduser admin)")


def is_admin(username: str | None) -> bool:
    return not ENABLED or username in ADMINS


def _key() -> bytes:
    if not _KEY.exists():
        SECRETS.mkdir(parents=True, exist_ok=True)
        _KEY.write_bytes(secrets.token_bytes(32))
    return _KEY.read_bytes()


def _sign(payload: str) -> str:
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(username: str) -> str:
    payload = f"{base64.urlsafe_b64encode(username.encode()).decode()}.{int(time.time()) + SESSION_TTL}"
    return f"{payload}.{_sign(payload)}"


def read_token(token: str | None) -> str | None:
    """Username for a valid, unexpired token whose user still exists."""
    try:
        name_b64, exp, sig = (token or "").split(".")
        if not hmac.compare_digest(sig, _sign(f"{name_b64}.{exp}")) or int(exp) < time.time():
            return None
        username = base64.urlsafe_b64decode(name_b64).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return username if username in _users() else None


def _cli(argv: list[str]) -> None:
    cmd, *rest = argv or ["help"]
    if cmd == "adduser" and rest:
        pw = getpass.getpass(f"Password for {rest[0]}: ")
        if not pw or pw != getpass.getpass("Repeat: "):
            sys.exit("Passwords are empty or do not match")
        set_password(rest[0], pw)
        print(f"User '{rest[0]}' saved")
    elif cmd == "deluser" and rest:
        print("Deleted" if delete_user(rest[0]) else "No such user")
    elif cmd == "list":
        print("\n".join(_users()) or "(no users)")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli(sys.argv[1:])
