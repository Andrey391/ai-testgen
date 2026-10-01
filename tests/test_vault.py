"""Secrets (stage 5.3): encryption of the files with TESTGEN_SECRET_KEY, HashiCorp Vault, who sees them."""
from __future__ import annotations

import json
import shutil
import uuid

import httpx
import pytest

from testgen import auth, projects, vault

KEY = "correct horse battery staple 42"


@pytest.fixture(autouse=True)
def secrets_dir(tmp_path, monkeypatch):
    """A folder of its own: the key must not encrypt the secrets other tests read."""
    monkeypatch.setattr(vault, "SECRETS", tmp_path)
    return tmp_path


@pytest.fixture
def kind():
    return f"projects/vault-{uuid.uuid4().hex[:6]}"


def _files(k: str) -> list:
    return list((vault.SECRETS / k).glob("*.json"))


def test_files_are_encrypted_with_the_key(monkeypatch, kind):
    monkeypatch.setenv(vault.KEY_ENV, KEY)
    vault.save(kind, "app", {"username": "user", "password": "S3cret-pass"})
    raw = _files(kind)[0].read_text("utf-8")
    assert "S3cret-pass" not in raw and "user" not in raw and json.loads(raw)["enc"] == "aes-256-gcm"
    assert vault.load(kind, "app") == {"username": "user", "password": "S3cret-pass"}
    assert vault.describe() == {"backend": "files", "encrypted": True}

    # The (kind, key) is authenticated: a file copied over another secret does not decrypt.
    vault.save(kind, "other", {"password": "x"})
    shutil.copy(vault.SECRETS / kind / "app.json", vault.SECRETS / kind / "other.json")
    with pytest.raises(vault.VaultError, match="подменён"):
        vault.load(kind, "other")
    monkeypatch.setenv(vault.KEY_ENV, "another key, long enough")
    with pytest.raises(vault.VaultError, match="расшифровать"):
        vault.load(kind, "app")
    monkeypatch.delenv(vault.KEY_ENV)
    with pytest.raises(vault.VaultError, match="не задан"):
        vault.load(kind, "app")
    monkeypatch.setenv(vault.KEY_ENV, "short")
    with pytest.raises(vault.VaultError, match="короче"):
        vault.save(kind, "app", {})


def test_plain_files_are_encrypted_on_read_and_by_the_cli(monkeypatch, kind):
    monkeypatch.delenv(vault.KEY_ENV, raising=False)
    vault.save(kind, "a", {"password": "plain-a"})
    vault.save(kind, "b", {"password": "plain-b"})
    assert "plain-a" in (vault.SECRETS / kind / "a.json").read_text("utf-8")
    monkeypatch.setenv(vault.KEY_ENV, KEY)
    assert vault.load(kind, "a") == {"password": "plain-a"}          # read, then encrypted in place
    assert "plain-a" not in (vault.SECRETS / kind / "a.json").read_text("utf-8")
    vault._cli(["encrypt"])
    assert "plain-b" not in (vault.SECRETS / kind / "b.json").read_text("utf-8")
    # Rotation: the old key in TESTGEN_SECRET_KEY_OLD, the new one in TESTGEN_SECRET_KEY.
    monkeypatch.setenv("TESTGEN_SECRET_KEY_OLD", KEY)
    monkeypatch.setenv(vault.KEY_ENV, "a brand new secret key")
    vault._cli(["rotate"])
    assert vault.load(kind, "b") == {"password": "plain-b"}


def test_project_secrets_never_on_disk_in_plain_text(monkeypatch):
    monkeypatch.setenv(vault.KEY_ENV, KEY)
    p = projects.create(f"Шифр {uuid.uuid4().hex[:6]}")
    projects.set_app_credentials(p["id"], "login-x", "Pa55word-on-disk", "JBSWY3DPEHPK3PXP")
    assert projects.app_credentials(p["id"])["password"] == "Pa55word-on-disk"
    for f in vault.SECRETS.rglob("*"):
        if f.is_file():
            text = f.read_bytes()
            assert b"Pa55word-on-disk" not in text and b"JBSWY3DPEHPK3PXP" not in text, f
    projects.delete(p["id"])
    assert not (vault.SECRETS / "projects" / p["id"]).exists()


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
        assert not (vault.SECRETS / "projects" / p["id"]).exists() and set(fake.tokens) == {"s.root-token"}
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
