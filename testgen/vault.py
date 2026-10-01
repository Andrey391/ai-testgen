"""Secrets kept on the server, outside data/ so they never end up in tests,
exports or the test list: logins for the applications under test, tokens of
connections, keys of model providers, the studio's session key.

A secret is a small JSON object addressed by (kind, key), e.g. ("projects/<id>", "app").
Where it lives:

- files (default): secrets/<kind>/<key>.json. With TESTGEN_SECRET_KEY set every file is
  encrypted: AES-256-GCM, the key is SHA-256 of the variable, the file's (kind, key) is
  authenticated data, so an encrypted file copied over another one does not decrypt.
  Plain files written before the key was set are still read and are encrypted on first
  read; `python -m testgen.vault encrypt` encrypts all of them at once,
  `python -m testgen.vault rotate` re-encrypts with a new key (the old one in
  TESTGEN_SECRET_KEY_OLD). Protect the folder with file-system permissions; do not commit it.
- HashiCorp Vault (TESTGEN_VAULT_ADDR): KV v2 engine TESTGEN_VAULT_MOUNT (default "secret"),
  under TESTGEN_VAULT_PREFIX (default "testgen"); token in TESTGEN_VAULT_TOKEN, namespace in
  TESTGEN_VAULT_NAMESPACE, CA bundle in TESTGEN_VAULT_CACERT. Nothing is written to disk then.

With a shared database (fs.py) the "files" live in its rows; there they must be encrypted:
without TESTGEN_SECRET_KEY (or HashiCorp Vault) saving a secret is refused.
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

from . import fs
from .paths import SECRETS

KEY_ENV = "TESTGEN_SECRET_KEY"
MIN_KEY = 16
TRANSPORT: httpx.BaseTransport | None = None     # tests: a fake Vault server


class VaultError(Exception):
    pass


def _name(key: str) -> str:
    return re.sub(r"[^\w@.-]+", "_", key.strip()) or "default"


def _kind(kind: str) -> str:
    return "/".join(_name(part) for part in kind.split("/") if part.strip())


# ---------- encryption of files ----------

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


class FileVault:
    name = "files"

    def __init__(self, root: Path):
        self.root = root

    def _path(self, kind: str, key: str) -> Path:
        return self.root / _kind(kind) / f"{_name(key)}.json"

    @staticmethod
    def _aad(kind: str, key: str) -> str:
        return f"{_kind(kind)}/{_name(key)}"

    def _write(self, p: Path, blob: dict) -> None:
        if fs.key(p) is not None:
            if not encrypted(blob):
                raise VaultError(f"В общей базе секреты хранятся только зашифрованными: задайте {KEY_ENV} "
                                 "(или TESTGEN_VAULT_ADDR)")
            fs.write_json(p, blob)
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{_random.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(blob, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, p)

    def load(self, kind: str, key: str) -> dict | None:
        p = self._path(kind, key)
        blob = fs.read_json(p)
        if blob is None:
            return None
        secret = os.environ.get(KEY_ENV)
        data = _open(blob, self._aad(kind, key), secret)
        if secret and not encrypted(blob):
            self._write(p, _seal(data, self._aad(kind, key), secret))    # written before the key was set
        return data

    def save(self, kind: str, key: str, data: dict) -> None:
        self._write(self._path(kind, key), _seal(data, self._aad(kind, key), os.environ.get(KEY_ENV)))

    def delete(self, kind: str, key: str) -> bool:
        return fs.unlink(self._path(kind, key))

    def delete_all(self, kind: str) -> None:
        if _kind(kind):
            fs.rmtree(self.root / _kind(kind))

    def reseal(self, old: str | None, new: str | None) -> int:
        """Re-encrypt every secret file: `old` key (None = plain or the same) -> `new`. -> files changed."""
        n = 0
        if fs.key(self.root) is None:
            files = list(self.root.rglob("*.json"))
        else:
            files = [p for depth in range(2, 6) for p in fs.glob(self.root, "/".join(["*"] * depth))]
        for p in sorted(files):
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
    wanted = ("hashicorp", addr) if addr else ("files", str(SECRETS))
    if _backend is None or _backend[0] != wanted:
        if addr:
            inst = HashiVault(addr, os.environ.get("TESTGEN_VAULT_TOKEN", ""),
                              os.environ.get("TESTGEN_VAULT_MOUNT", "secret"),
                              os.environ.get("TESTGEN_VAULT_PREFIX", "testgen"),
                              os.environ.get("TESTGEN_VAULT_NAMESPACE", ""),
                              os.environ.get("TESTGEN_VAULT_CACERT", ""))
        else:
            inst = FileVault(SECRETS)
        _backend = (wanted, inst)
    return _backend[1]


def describe() -> dict:
    """For the admin and the security team: where secrets are and whether they are encrypted."""
    b = backend()
    return {"backend": b.name, "encrypted": b.name != "files" or bool(os.environ.get(KEY_ENV))}


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
    cmd = (argv or ["help"])[0]
    files = FileVault(SECRETS)
    if cmd == "encrypt":
        key = os.environ.get(KEY_ENV)
        if not key:
            sys.exit(f"Задайте {KEY_ENV}")
        print(f"Зашифровано файлов: {files.reseal(key, key)}")
    elif cmd == "rotate":
        old, new = os.environ.get("TESTGEN_SECRET_KEY_OLD"), os.environ.get(KEY_ENV)
        if not old or not new:
            sys.exit(f"Задайте старый ключ в TESTGEN_SECRET_KEY_OLD и новый в {KEY_ENV}")
        print(f"Перешифровано файлов: {files.reseal(old, new)}")
    elif cmd == "decrypt":
        print(f"Расшифровано файлов: {files.reseal(os.environ.get(KEY_ENV), None)}")
    elif cmd == "status":
        print(describe())
    else:
        print(__doc__ + "\nCommands: encrypt | rotate | decrypt | status")


if __name__ == "__main__":
    _cli(sys.argv[1:])
