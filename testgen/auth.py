"""Login to the studio itself.

Users live in secrets/users.json (PBKDF2 hashes). The browser gets an
HMAC-signed, HttpOnly session cookie. On first start with no users an
`admin` account is created and its password printed to the console.

Manage users:  python -m testgen.auth adduser <name>
               python -m testgen.auth deluser <name>
               python -m testgen.auth list
               python -m testgen.auth token <name> [token name]
Set TESTGEN_AUTH=off to disable login (single-user local use).
Anyone who can open the studio may register via the login screen; set
TESTGEN_SIGNUP=off to allow only accounts created from the CLI.

Corporate login (sso.py): OIDC or LDAP. Their users are added here on first login
with the groups the directory reported (no password hash); with SSO configured,
self-registration and local passwords are off unless TESTGEN_SIGNUP=on /
TESTGEN_LOCAL_LOGIN=on, and a session lasts TESTGEN_SSO_SESSION_HOURS (12).

API tokens let IDE agents use the studio as an MCP server (mcp_server.py) on
behalf of a user: "tg_<id>_<secret>", only a SHA-256 of the secret is kept in
secrets/tokens.json. A token carries its user's rights, nothing more.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import getpass
import hashlib
import hmac
import os
import re
import secrets
import sys
import time

from . import db, fs, sso, vault
from .paths import DATA
from .vault import SECRETS

_OFF = ("off", "0", "false", "no")
ENABLED = os.environ.get("TESTGEN_AUTH", "on").lower() not in _OFF
SSO = sso.oidc_enabled() or sso.ldap_enabled()
SIGNUP = ENABLED and os.environ.get("TESTGEN_SIGNUP", "off" if SSO else "on").lower() not in _OFF
# Passwords of the studio's own users (secrets/users.json).
LOCAL_LOGIN = not ENABLED or os.environ.get("TESTGEN_LOCAL_LOGIN", "off" if SSO else "on").lower() not in _OFF
SSO_SESSION_TTL = int(float(os.environ.get("TESTGEN_SSO_SESSION_HOURS", "12")) * 3600)
# Admins may set commands and environment of MCP connections (that is code
# execution on the server). Comma-separated usernames.
ADMINS = {u.strip() for u in os.environ.get("TESTGEN_ADMINS", "admin").split(",") if u.strip()}
COOKIE = "tg_session"
SESSION_TTL = 7 * 24 * 3600
ANONYMOUS = "local"

_USERS = SECRETS / "users.json"
_KEY = SECRETS / "session.key"
_TOKENS = SECRETS / "tokens.json"
_ITERATIONS = 200_000
# Usernames key per-user files in secrets/ (vault._path), so keep them to the
# characters it leaves as is: otherwise "a b" and "a_b" would share a file.
USERNAME = re.compile(r"[\w@.-]{1,64}")
# Studio-wide: directory groups -> roles in projects, groups of studio admins (access.py).
_SSO = DATA / "sso.json"
MIN_PASSWORD = 8


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS).hex()


# users.json and sso.json are read on every request (the session, the role in each project): kept
# for a second; a write in this process forgets them at once, other instances see it within CACHE_TTL.
CACHE_TTL = 1.0
_cache: dict[tuple, tuple[float, object]] = {}


def _cached(path, read):
    key = (str(path), db.url())
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return copy.deepcopy(hit[1])
    value = read()
    _cache[key] = (time.time(), value)
    return copy.deepcopy(value)


def _forget(path) -> None:
    for key in [k for k in _cache if k[0] == str(path)]:
        _cache.pop(key, None)


def _read_json(path) -> dict:
    return fs.read_json(path, {})


def _users() -> dict:
    return _cached(_USERS, lambda: _read_json(_USERS))


def _write_users(users: dict) -> None:
    fs.write_json(_USERS, users)
    _forget(_USERS)


@contextlib.contextmanager
def _editing(path):
    """Read-modify-write of users.json / tokens.json: every instance of the studio writes them."""
    with fs.lock(path):
        data = fs.read_json(path, {})
        yield data
        fs.write_json(path, data)
        _forget(path)


def set_password(username: str, password: str) -> None:
    salt = secrets.token_hex(16)
    with _editing(_USERS) as users:
        users[username] = (users.get(username) or {}) | {"salt": salt, "hash": _hash(password, salt)}


def register(username: str, password: str) -> None:
    """Self-registration from the login screen; ValueError explains a refusal."""
    if not USERNAME.fullmatch(username):
        raise ValueError("Логин: до 64 символов, буквы, цифры и . _ @ -")
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Пароль должен быть не короче {MIN_PASSWORD} символов")
    if username in _users():
        raise ValueError("Такой пользователь уже есть")
    set_password(username, password)


def delete_user(username: str) -> bool:
    with _editing(_USERS) as users:
        return users.pop(username, None) is not None


def verify(username: str, password: str) -> bool:
    u = _users().get(username)
    if not u or not u.get("hash"):
        _hash(password, "00" * 16)   # same timing whether or not the user exists
        return False
    return hmac.compare_digest(u["hash"], _hash(password, u["salt"]))


def ensure_admin() -> None:
    """First start: create `admin` with a random password so the studio is never open
    (not with SSO: admins then come from TESTGEN_ADMINS or admin groups of the directory)."""
    if not (ENABLED and LOCAL_LOGIN):
        return
    with fs.lock(_USERS):          # instances starting together: only one of them creates it
        if _users():
            return
        password = secrets.token_urlsafe(12)
        set_password("admin", password)
    print(f"Created studio user 'admin' with password: {password}\n"
          f"(change it: python -m testgen.auth adduser admin)")


def is_admin(username: str | None) -> bool:
    if not ENABLED:
        return True
    if not username:
        return False
    return username in ADMINS or bool(set(user_groups(username)) & set(sso_settings()["admin_groups"]))


def list_users() -> list[str]:
    return sorted(_users())


def user_groups(username: str | None) -> list[str]:
    """Directory groups of a user, as the last SSO or LDAP login reported them."""
    return list((_users().get(username or "") or {}).get("groups") or [])


def sso_settings() -> dict:
    def read():
        try:
            return fs.read_json(_SSO, {})
        except ValueError:
            return {}
    s = _cached(_SSO, read)
    return {"group_roles": list(s.get("group_roles") or []), "admin_groups": list(s.get("admin_groups") or [])}


def save_sso_settings(s: dict) -> dict:
    s = {"group_roles": s.get("group_roles") or [], "admin_groups": s.get("admin_groups") or []}
    fs.write_json(_SSO, s)
    _forget(_SSO)
    return s


_session_key: bytes | None = None


def _key() -> bytes:
    """The HMAC key of session cookies: a secret of the vault (encrypted, or in HashiCorp Vault, so
    every instance of the studio shares it); secrets/session.key of older versions moves there."""
    global _session_key
    if _session_key is None:
        with fs.lock(_KEY):        # instances starting together agree on one key
            stored = vault.load("studio", "session")
            if stored:
                key = bytes.fromhex(stored["key"])
            else:
                key = fs.read_bytes(_KEY) if fs.is_file(_KEY) else secrets.token_bytes(32)
                vault.save("studio", "session", {"key": key.hex()})
                fs.unlink(_KEY)
        _session_key = key
    return _session_key


def _sign(payload: str) -> str:
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(username: str, ttl: int | None = None) -> str:
    payload = f"{base64.urlsafe_b64encode(username.encode()).decode()}.{int(time.time()) + (ttl or SESSION_TTL)}"
    return f"{payload}.{_sign(payload)}"


def sso_login(username: str, groups: list[str], source: str) -> str:
    """A user the directory (OIDC, LDAP) let in: kept with their current groups. -> the username."""
    if not USERNAME.fullmatch(username or ""):
        raise sso.SsoError(f"Логин «{username}» из каталога не подходит студии: до 64 символов, буквы, цифры и . _ @ -")
    with _editing(_USERS) as users:
        users[username] = (users.get(username) or {}) | {"sso": source, "groups": groups, "last_login": time.time()}
    return username


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


# ---------- API tokens (MCP access from IDEs) ----------

def _tokens() -> dict:
    return fs.read_json(_TOKENS, {})


def create_api_token(username: str, name: str = "") -> tuple[str, dict]:
    """-> (the token, shown once; its public record)."""
    tid, secret = secrets.token_hex(4), secrets.token_urlsafe(24)
    rec = {"id": tid, "user": username, "name": (name or "IDE").strip()[:60], "created": time.time(),
           "hash": hashlib.sha256(secret.encode()).hexdigest(), "last_used": None}
    with _editing(_TOKENS) as tokens:
        tokens[tid] = rec
    return f"tg_{tid}_{secret}", {k: v for k, v in rec.items() if k != "hash"}


def list_api_tokens(username: str) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "hash"} for r in _tokens().values() if r["user"] == username]


def delete_api_token(username: str, tid: str) -> bool:
    with _editing(_TOKENS) as tokens:
        if tokens.get(tid, {}).get("user") != username:
            return False
        del tokens[tid]
    return True


def user_for_api_token(token: str | None) -> str | None:
    m = re.fullmatch(r"tg_([0-9a-f]{8})_([\w-]{20,})", (token or "").strip())
    if not m:
        return None
    tokens = _tokens()
    rec = tokens.get(m.group(1))
    if not rec or not hmac.compare_digest(rec["hash"], hashlib.sha256(m.group(2).encode()).hexdigest()):
        return None
    if ENABLED and rec["user"] not in _users():
        return None
    if not rec.get("last_used") or time.time() - rec["last_used"] > 60:
        with _editing(_TOKENS) as fresh:
            if m.group(1) in fresh:
                fresh[m.group(1)]["last_used"] = time.time()
    return rec["user"]


def _audit(action: str, username: str, details: dict | None = None) -> None:
    from . import audit
    audit.record(action, user=os.environ.get("TESTGEN_CLI_USER") or getpass.getuser(), target={"user": username},
                 details=details, via="cli")


def _cli(argv: list[str]) -> None:
    cmd, *rest = argv or ["help"]
    if cmd == "adduser" and rest:
        pw = getpass.getpass(f"Password for {rest[0]}: ")
        if not pw or pw != getpass.getpass("Repeat: "):
            sys.exit("Passwords are empty or do not match")
        set_password(rest[0], pw)
        _audit("user.create", rest[0])
        print(f"User '{rest[0]}' saved")
    elif cmd == "deluser" and rest:
        ok = delete_user(rest[0])
        if ok:
            _audit("user.delete", rest[0])
        print("Deleted" if ok else "No such user")
    elif cmd == "list":
        print("\n".join(_users()) or "(no users)")
    elif cmd == "token" and rest:
        if ENABLED and rest[0] not in _users():
            sys.exit(f"No such user: {rest[0]}")
        token, rec = create_api_token(rest[0], " ".join(rest[1:]) or "CLI")
        _audit("token.create", rest[0], {"token": rec["id"]})
        print(f"API token for {rest[0]} (shown once): {token}")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli(sys.argv[1:])
