"""Secrets (stage 5.3): always encrypted in the database (TESTGEN_SECRET_KEY or the key of local
development), HashiCorp Vault, who sees them."""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from testgen import auth, db, fs, projects, vault

KEY = "correct horse battery staple 42"


@pytest.fixture(autouse=True)
def key(monkeypatch):
    """A key and a root of its own: other tests read their secrets with the key of conftest.py, and
    `vault encrypt` / `rotate` go over every secret under the root."""
    monkeypatch.setenv(vault.KEY_ENV, KEY)
    monkeypatch.setattr(vault, "SECRETS", vault.SECRETS / f"vault-tests-{uuid.uuid4().hex[:6]}")


@pytest.fixture
def kind():
    return f"projects/vault-{uuid.uuid4().hex[:6]}"


def _file(kind: str, key: str):
    return vault.SECRETS / kind / f"{key}.json"


def _rows(kind: str) -> list:
    """Every row of the database under a kind of secrets: what an administrator of PostgreSQL sees."""
    prefix = fs.key(vault.SECRETS / kind) + "/"
    t = db.docs
    with db.engine().connect() as c:
        return list(c.execute(select(t.c.path, t.c.body, t.c.data).where(t.c.path.like(prefix + "%"))))


def test_secrets_are_encrypted_with_the_key(monkeypatch, kind):
    vault.save(kind, "app", {"username": "user", "password": "S3cret-pass"})
    raw = fs.read_text(_file(kind, "app"))
    assert "S3cret-pass" not in raw and "user" not in raw and json.loads(raw)["enc"] == "aes-256-gcm"
    assert vault.load(kind, "app") == {"username": "user", "password": "S3cret-pass"}
    assert vault.describe() == {"backend": "database", "encrypted": True, "key": "env"}

    # The (kind, key) is authenticated: a secret copied over another one does not decrypt.
    vault.save(kind, "other", {"password": "x"})
    fs.write_text(_file(kind, "other"), raw)
    with pytest.raises(vault.VaultError, match="подменён"):
        vault.load(kind, "other")
    monkeypatch.setenv(vault.KEY_ENV, "another key, long enough")
    with pytest.raises(vault.VaultError, match="расшифровать"):
        vault.load(kind, "app")
    monkeypatch.setenv(vault.KEY_ENV, "short")
    with pytest.raises(vault.VaultError, match="короче"):
        vault.save(kind, "app", {})


def test_without_the_key(monkeypatch, tmp_path, kind):
    """A database on this computer: the key of local development, made once; elsewhere - refused."""
    vault.save(kind, "app", {"password": "p"})
    monkeypatch.delenv(vault.KEY_ENV)
    monkeypatch.setattr(vault, "LOCAL_KEY", tmp_path / "secret.key")
    vault.save(kind, "local", {"password": "local-p"})
    made = (tmp_path / "secret.key").read_text("utf-8")
    assert len(made) >= 32 and vault.load(kind, "local") == {"password": "local-p"}
    assert vault.secret_key() == made and vault.describe()["key"] == "local"
    with pytest.raises(vault.VaultError, match="расшифровать"):        # saved with another key
        vault.load(kind, "app")

    monkeypatch.setenv("TESTGEN_DATABASE_URL", "postgresql+psycopg://u:p@db.corp:5432/testgen")
    with pytest.raises(vault.VaultError, match="Задайте TESTGEN_SECRET_KEY"):
        vault.check()                   # the studio does not start (db.check)


def test_plain_secrets_are_encrypted_on_read_and_by_the_cli(monkeypatch, kind):
    """Secrets of an older version brought by `db import-files` may be plain."""
    fs.write_json(_file(kind, "a"), {"password": "plain-a"})
    fs.write_json(_file(kind, "b"), {"password": "plain-b"})
    assert vault.load(kind, "a") == {"password": "plain-a"}          # read, then encrypted in place
    assert "plain-a" not in fs.read_text(_file(kind, "a"))
    vault._cli(["encrypt"])
    assert "plain-b" not in fs.read_text(_file(kind, "b"))
    # Rotation: the old key in TESTGEN_SECRET_KEY_OLD, the new one in TESTGEN_SECRET_KEY.
    monkeypatch.setenv("TESTGEN_SECRET_KEY_OLD", KEY)
    monkeypatch.setenv(vault.KEY_ENV, "a brand new secret key")
    vault._cli(["rotate"])
    assert vault.load(kind, "b") == {"password": "plain-b"}
    with pytest.raises(vault.VaultError, match="только зашифрованными"):
        vault.backend()._write(_file(kind, "c"), {"password": "plain"})


def test_project_secrets_never_in_the_database_in_plain_text():
    p = projects.create(f"Шифр {uuid.uuid4().hex[:6]}")
    projects.set_app_credentials(p["id"], "login-x", "Pa55word-on-disk", "JBSWY3DPEHPK3PXP")
    assert projects.app_credentials(p["id"])["password"] == "Pa55word-on-disk"
    rows = _rows(f"projects/{p['id']}")
    assert rows
    for r in rows:
        text = (r.body or "").encode() + bytes(r.data or b"")
        assert b"Pa55word-on-disk" not in text and b"JBSWY3DPEHPK3PXP" not in text, r.path
    projects.delete(p["id"])
    assert not _rows(f"projects/{p['id']}")


class FakeHashiVault:
    """KV v2 of HashiCorp Vault: /v1/<mount>/data/<path> and /v1/<mount>/metadata/<path>."""

    def __init__(self):
        self.data: dict[str, dict] = {}
        self.tokens = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.tokens.append(request.headers.get("X-Vault-Token"))
        path = request.url.path
        assert path.startswith("/v1/kv/")
        what, _, rest = path[len("/v1/kv/"):].partition("/")
        if what == "data" and request.method == "GET":
            return httpx.Response(200, json={"data": {"data": self.data[rest]}}) if rest in self.data \
                else httpx.Response(404, json={"errors": []})
        if what == "data" and request.method == "POST":
            self.data[rest] = json.loads(request.content)["data"]
            return httpx.Response(200, json={})
        if what == "metadata" and request.method == "DELETE":
            self.data.pop(rest, None)
            return httpx.Response(204)
        if what == "metadata" and request.method == "LIST":
            keys = sorted({k[len(rest) + 1:].split("/")[0] + ("/" if "/" in k[len(rest) + 1:] else "")
                           for k in self.data if k.startswith(rest + "/")})
            return httpx.Response(200, json={"data": {"keys": keys}}) if keys else httpx.Response(404, json={})
        return httpx.Response(405)


def test_hashicorp_vault_backend(monkeypatch):
    fake = FakeHashiVault()
    monkeypatch.setattr(vault, "TRANSPORT", httpx.MockTransport(fake))
    monkeypatch.setenv("TESTGEN_VAULT_ADDR", "https://vault.corp:8200")
    monkeypatch.setenv("TESTGEN_VAULT_TOKEN", "s.root-token")
    monkeypatch.setenv("TESTGEN_VAULT_MOUNT", "kv")
    try:
        p = projects.create(f"Vault {uuid.uuid4().hex[:6]}")
        projects.set_app_credentials(p["id"], "login-v", "in-vault-only")
        assert fake.data[f"testgen/projects/{p['id']}/app"] == {"username": "login-v", "password": "in-vault-only"}
        assert projects.app_credentials(p["id"])["password"] == "in-vault-only"
        assert not _rows(f"projects/{p['id']}") and set(fake.tokens) == {"s.root-token"}
        vault.save(f"projects/{p['id']}/nested", "x", {"a": 1})
        projects.delete(p["id"])
        assert not [k for k in fake.data if p["id"] in k]
        assert vault.describe() == {"backend": "hashicorp", "encrypted": True}
        # The studio's session key is shared by every instance through the vault.
        monkeypatch.setattr(auth, "_session_key", None)
        token = auth.make_token("someone")
        assert "testgen/studio/session" in fake.data
        monkeypatch.setattr(auth, "_session_key", None)
        assert auth._sign("x") == auth._sign("x") and token.endswith(auth._sign(token.rsplit(".", 1)[0]))
    finally:
        monkeypatch.delenv("TESTGEN_VAULT_ADDR")
        monkeypatch.setattr(auth, "_session_key", None)
