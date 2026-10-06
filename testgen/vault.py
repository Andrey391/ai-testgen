"""Secrets kept apart from the data, so they never end up in tests, exports or the test list:
logins for the applications under test, tokens of connections, keys of model providers, the
studio's session key.

A secret is a small JSON object addressed by (kind, key), e.g. ("projects/<id>", "app").
Where it lives:

- the database (default): rows of `docs` by the path secrets/<kind>/<key>.json (fs.py), always
  encrypted: AES-256-GCM, the key is SHA-256 of TESTGEN_SECRET_KEY, the secret's (kind, key) is
  authenticated data, so an encrypted row copied over another one does not decrypt. A plain secret
  (brought by `db import-files`) is encrypted on first read; `python -m testgen.vault encrypt`
  encrypts all of them at once, `python -m testgen.vault rotate` re-encrypts with a new key (the old
  one in TESTGEN_SECRET_KEY_OLD).
  Without TESTGEN_SECRET_KEY nothing starts - except with a database on this computer (local
  development): then the key is made once and kept in %LOCALAPPDATA%\\aitestgen\\secret.key
  (~/.cache/aitestgen/secret.key elsewhere). Lose that file and the secrets are lost with it.
- HashiCorp Vault (TESTGEN_VAULT_ADDR): KV v2 engine TESTGEN_VAULT_MOUNT (default "secret"),
  under TESTGEN_VAULT_PREFIX (default "testgen"); token in TESTGEN_VAULT_TOKEN, namespace in
  TESTGEN_VAULT_NAMESPACE, CA bundle in TESTGEN_VAULT_CACERT. Nothing is written to the database then.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets as _random
import sys
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import db, fs
from .paths import HOME, SECRETS

KEY_ENV = "TESTGEN_SECRET_KEY"
MIN_KEY = 16
LOCAL_KEY = HOME / "secret.key"
LOCAL_HOSTS = ("", "localhost", "127.0.0.1", "::1")
TRANSPORT: httpx.BaseTransport | None = None     # tests: a fake Vault server


class VaultError(Exception):
    pass


def _name(key: str) -> str:
    return re.sub(r"[^\w@.-]+", "_", key.strip()) or "default"


def _kind(kind: str) -> str:
    return "/".join(_name(part) for part in kind.split("/") if part.strip())


# ---------- the key ----------

def secret_key() -> str:
    """TESTGEN_SECRET_KEY; with a database on this computer and no variable - the key of local
    development (LOCAL_KEY), made on first use."""
    key = os.environ.get(KEY_ENV, "").strip()
    if key:
        return key
    from sqlalchemy.engine import make_url
    try:
        host = make_url(db.url()).host or ""
    except db.NotConfigured:
        host = None
    if host not in LOCAL_HOSTS:
        raise VaultError(f"Задайте {KEY_ENV} — длинную случайную строку: секреты в базе хранятся только "
                         "зашифрованными (или TESTGEN_VAULT_ADDR для HashiCorp Vault)")
    if not LOCAL_KEY.is_file():
        LOCAL_KEY.parent.mkdir(parents=True, exist_ok=True)
        tmp = LOCAL_KEY.with_name(f".{LOCAL_KEY.name}.{_random.token_hex(4)}")
        tmp.write_text(_random.token_urlsafe(36), "utf-8")
        if os.name != "nt":
            tmp.chmod(0o600)
        try:
            os.link(tmp, LOCAL_KEY)          # two processes starting together: the first key wins
        except FileExistsError:
            pass
        finally:
            tmp.unlink()
        if LOCAL_KEY.is_file():
            print(f"Ключ шифрования секретов локальной разработки: {LOCAL_KEY} (без него секреты в базе "
                  f"не прочитать; для сервера задайте {KEY_ENV})", file=sys.stderr)
    return LOCAL_KEY.read_text("utf-8").strip()


# ---------- encryption ----------

def _aes(secret: str | None) -> AESGCM | None:
    if not secret:
        return None
    if len(secret) < MIN_KEY:
        raise VaultError(f"{KEY_ENV} короче {MIN_KEY} символов: задайте длинную случайную строку")
    return AESGCM(hashlib.sha256(secret.encode()).digest())


def encrypted(blob: dict) -> bool:
    return isinstance(blob, dict) and blob.get("enc") == "aes-256-gcm" and "data" in blob


def _seal(data: dict, aad: str, secret: str | None) -> dict:
    aes = _aes(secret)
    if not aes:
        return data
    nonce = _random.token_bytes(12)
    box = aes.encrypt(nonce, json.dumps(data, ensure_ascii=False).encode(), aad.encode())
    return {"enc": "aes-256-gcm", "v": 1, "nonce": base64.b64encode(nonce).decode(),
            "data": base64.b64encode(box).decode()}


def _open(blob: dict, aad: str, secret: str | None) -> dict:
    if not encrypted(blob):
        return blob
    aes = _aes(secret)
    if not aes:
        raise VaultError(f"Секрет {aad} зашифрован, а {KEY_ENV} не задан")
    try:
        plain = aes.decrypt(base64.b64decode(blob["nonce"]), base64.b64decode(blob["data"]), aad.encode())
    except (InvalidTag, ValueError):
        raise VaultError(f"Не удалось расшифровать секрет {aad}: другой {KEY_ENV} или файл подменён")
    return json.loads(plain)


class DbVault:
    """Secrets as encrypted documents of the database (fs.py), by path: SECRETS/<kind>/<key>.json."""
    name = "database"

    def __init__(self, root: Path):
        self.root = root

    def _path(self, kind: str, key: str) -> Path:
        return self.root / _kind(kind) / f"{_name(key)}.json"

    @staticmethod
    def _aad(kind: str, key: str) -> str:
        return f"{_kind(kind)}/{_name(key)}"

    @staticmethod
    def _write(p: Path, blob: dict) -> None:
        if not encrypted(blob):
            raise VaultError(f"Секреты хранятся только зашифрованными: задайте {KEY_ENV}")
        fs.write_json(p, blob)

    def load(self, kind: str, key: str) -> dict | None:
        p = self._path(kind, key)
        blob = fs.read_json(p)
        if blob is None:
            return None
        secret = secret_key()
        data = _open(blob, self._aad(kind, key), secret)
        if not encrypted(blob):
            self._write(p, _seal(data, self._aad(kind, key), secret))    # brought plain by import-files
        return data

    def save(self, kind: str, key: str, data: dict) -> None:
        self._write(self._path(kind, key), _seal(data, self._aad(kind, key), secret_key()))

    def delete(self, kind: str, key: str) -> bool:
        return fs.unlink(self._path(kind, key))

    def delete_all(self, kind: str) -> None:
        if _kind(kind):
            fs.rmtree(self.root / _kind(kind))

    def reseal(self, old: str | None, new: str) -> int:
        """Re-encrypt every secret: `old` key (None = plain or the same) -> `new`. -> secrets changed."""
        n = 0
        for p in sorted(p for depth in range(2, 6) for p in fs.glob(self.root, "/".join(["*"] * depth))):
            rel = p.relative_to(self.root)
            if len(rel.parts) < 2 or p.suffix != ".json":       # users.json, tokens.json: hashes, not secrets
                continue
            aad = "/".join(rel.parts[:-1]) + "/" + p.stem
            blob = fs.read_json(p)
            data = _open(blob, aad, old if encrypted(blob) else None)
            self._write(p, _seal(data, aad, new))
            n += 1
        return n


# ---------- HashiCorp Vault, KV v2 ----------

class HashiVault:
    name = "hashicorp"

    def __init__(self, addr: str, token: str, mount: str = "secret", prefix: str = "testgen",
                 namespace: str = "", cacert: str = ""):
        headers = {"X-Vault-Token": token}
        if namespace:
            headers["X-Vault-Namespace"] = namespace
        self.mount, self.prefix = mount.strip("/"), prefix.strip("/")
        self.http = httpx.Client(base_url=addr.rstrip("/"), headers=headers, timeout=15,
                                 verify=cacert or True, transport=TRANSPORT)

    def _path(self, what: str, kind: str, key: str = "") -> str:
        parts = [self.prefix, _kind(kind)] + ([_name(key)] if key else [])
        return f"/v1/{self.mount}/{what}/" + "/".join(p for p in parts if p)

    def _call(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            r = self.http.request(method, url, **kw)
        except httpx.HTTPError as e:
            raise VaultError(f"HashiCorp Vault недоступен: {e}")
        if r.status_code >= 400 and r.status_code != 404:
            raise VaultError(f"HashiCorp Vault ответил {r.status_code}: {r.text[:200]}")
        return r

    def load(self, kind: str, key: str) -> dict | None:
        r = self._call("GET", self._path("data", kind, key))
        return None if r.status_code == 404 else r.json()["data"]["data"]

    def save(self, kind: str, key: str, data: dict) -> None:
        self._call("POST", self._path("data", kind, key), json={"data": data})

    def delete(self, kind: str, key: str) -> bool:
        return self._call("DELETE", self._path("metadata", kind, key)).status_code < 400

    def _list(self, kind: str) -> list[str]:
        r = self._call("LIST", self._path("metadata", kind))
        return [] if r.status_code == 404 else r.json()["data"]["keys"]

    def delete_all(self, kind: str) -> None:
        for k in self._list(kind):
            if k.endswith("/"):
                self.delete_all(f"{kind}/{k[:-1]}")
            else:
                self.delete(kind, k)


# ---------- the backend in use ----------

_backend = None


def backend():
    global _backend
    addr = os.environ.get("TESTGEN_VAULT_ADDR", "").strip()
    wanted = ("hashicorp", addr) if addr else ("database", str(SECRETS))
    if _backend is None or _backend[0] != wanted:
        if addr:
            inst = HashiVault(addr, os.environ.get("TESTGEN_VAULT_TOKEN", ""),
                              os.environ.get("TESTGEN_VAULT_MOUNT", "secret"),
                              os.environ.get("TESTGEN_VAULT_PREFIX", "testgen"),
                              os.environ.get("TESTGEN_VAULT_NAMESPACE", ""),
                              os.environ.get("TESTGEN_VAULT_CACERT", ""))
        else:
            inst = DbVault(SECRETS)
        _backend = (wanted, inst)
    return _backend[1]


def check() -> None:
    """Secrets can be kept: the key is there (or HashiCorp Vault is used). -> VaultError."""
    if backend().name == "database":
        secret_key()


def describe() -> dict:
    """For the admin and the security team: where secrets are and whether they are encrypted."""
    b = backend()
    if b.name != "database":
        return {"backend": b.name, "encrypted": True}
    return {"backend": b.name, "encrypted": True, "key": "env" if os.environ.get(KEY_ENV, "").strip() else "local"}


def load(kind: str, key: str) -> dict | None:
    return backend().load(kind, key)


def save(kind: str, key: str, data: dict) -> None:
    backend().save(kind, key, data)


def delete(kind: str, key: str) -> bool:
    return backend().delete(kind, key)


def delete_all(kind: str) -> None:
    """Every secret of a kind, e.g. of a deleted project."""
    backend().delete_all(kind)


def _cli(argv: list[str]) -> None:
    from .paths import utf8_console
    utf8_console()
    cmd = (argv or ["help"])[0]
    secrets = DbVault(SECRETS)
    try:
        if cmd == "encrypt":
            key = secret_key()
            print(f"Зашифровано секретов: {secrets.reseal(key, key)}")
        elif cmd == "rotate":
            old, new = os.environ.get("TESTGEN_SECRET_KEY_OLD"), os.environ.get(KEY_ENV)
            if not old or not new:
                sys.exit(f"Задайте старый ключ в TESTGEN_SECRET_KEY_OLD и новый в {KEY_ENV}")
            print(f"Перешифровано секретов: {secrets.reseal(old, new)}")
        elif cmd == "status":
            print(describe())
        else:
            print(__doc__ + "\nCommands: encrypt | rotate | status")
    except (VaultError, db.NotConfigured) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    _cli(sys.argv[1:])
