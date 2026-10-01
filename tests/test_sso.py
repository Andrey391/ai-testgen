"""Corporate login (stage 5.2): OIDC against a fake identity provider, LDAP against ldap3's mock
directory; directory groups become roles in projects."""
from __future__ import annotations

import base64
import hashlib
import time
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import ldap3
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from testgen import audit, auth, projects, sso

ISSUER = "https://sso.test/realms/qa"


class FakeIdP:
    """Keycloak-like OIDC provider: discovery, JWKS, token and userinfo endpoints."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes: dict[str, dict] = {}
        self.claims = {"preferred_username": "ivan.petrov", "email": "ivan@corp.test", "groups": ["/qa-team"]}
        self.audience = "testgen"

    def authorize(self, url: str) -> str:
        """The user signs in at the provider: -> the code it sends back."""
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
        code = uuid.uuid4().hex
        self.codes[code] = q
        return code

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        base = "/realms/qa/protocol/openid-connect"
        if path == "/realms/qa/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
                "userinfo_endpoint": f"{ISSUER}/protocol/openid-connect/userinfo"})
        if path == f"{base}/certs":
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
            return httpx.Response(200, json={"keys": [jwk | {"kid": "k1", "alg": "RS256", "use": "sig"}]})
        if path == f"{base}/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            q = self.codes.pop(form["code"], None)
            if not q or form["client_secret"] != "s3cret":
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
            assert challenge.decode() == q["code_challenge"] and form["redirect_uri"] == q["redirect_uri"]
            now = int(time.time())
            token = jwt.encode({"iss": ISSUER, "aud": self.audience, "sub": "u-1", "iat": now, "exp": now + 300,
                                "nonce": q["nonce"]} | self.claims, self.key, algorithm="RS256", headers={"kid": "k1"})
            return httpx.Response(200, json={"id_token": token, "access_token": "at-1", "token_type": "Bearer"})
        return httpx.Response(404)


@pytest.fixture
def idp(monkeypatch):
    fake = FakeIdP()
    monkeypatch.setattr(sso, "TRANSPORT", httpx.MockTransport(fake))
    sso._discovery.clear()
    for k, v in {"TESTGEN_OIDC_ISSUER": ISSUER, "TESTGEN_OIDC_CLIENT_ID": "testgen",
                 "TESTGEN_OIDC_CLIENT_SECRET": "s3cret", "TESTGEN_OIDC_TITLE": "Keycloak"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "LOCAL_LOGIN", False)
    monkeypatch.setattr(audit, "DATA", audit.DATA.parent / f"audit-sso-{uuid.uuid4().hex[:6]}")
    yield fake
    auth.save_sso_settings({})


@pytest.fixture
def web():
    import server
    return TestClient(server.app, base_url="http://127.0.0.1:8765", follow_redirects=False)


def _oidc_login(web, idp, tamper=None) -> httpx.Response:
    r = web.get("/api/auth/oidc/login")
    assert r.status_code == 302 and r.headers["location"].startswith(f"{ISSUER}/protocol/openid-connect/auth?")
    code = idp.authorize(r.headers["location"])
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    if tamper:
        state = tamper(state)
    return web.get(f"/api/auth/oidc/callback?code={code}&state={state}")


def test_oidc_login_and_groups_to_roles(idp, web):
    me = web.get("/api/auth/me").json()
    assert me["sso"]["oidc"] == "Keycloak" and not me["local_login"]
    assert web.post("/api/auth/login", json={"username": "admin", "password": "x" * 10}).status_code == 403

    p = projects.create("SSO " + uuid.uuid4().hex[:6], owner="someone")
    auth.save_sso_settings({"group_roles": [{"group": "qa-team", "project": p["id"], "role": "editor"}]})
    r = _oidc_login(web, idp)
    assert r.status_code == 302 and r.headers["location"] == "/"
    me = web.get("/api/auth/me").json()
    assert me["user"] == "ivan.petrov" and me["groups"] == ["qa-team"]
    assert web.get(f"/api/projects/{p['id']}").json()["role"] == "editor"
    rec = audit.read(action="auth.login")[0]
    assert rec["user"] == "ivan.petrov" and rec["details"]["method"] == "oidc"

    # Groups are refreshed at every login: out of the group, out of the project.
    idp.claims["groups"] = []
    web.cookies.clear()
    _oidc_login(web, idp)
    assert web.get(f"/api/projects/{p['id']}").status_code == 404


def test_oidc_refusals(idp, web):
    r = _oidc_login(web, idp, tamper=lambda s: s + "x")
    assert r.status_code == 302 and "login_error=" in r.headers["location"] and "state" in r.headers["location"]
    web.cookies.clear()
    idp.audience = "another-client"                  # an ID token issued for someone else
    r = _oidc_login(web, idp)
    assert "login_error=" in r.headers["location"]
    assert web.get("/api/auth/me").json()["user"] is None
    r = web.get("/api/auth/oidc/callback?error=access_denied&error_description=denied")
    assert "login_error=" in r.headers["location"]


@pytest.fixture
def directory(monkeypatch):
    server = ldap3.Server("fake-dc")
    admin = ldap3.Connection(server, "cn=svc,dc=corp", "svc-pw", client_strategy=ldap3.MOCK_SYNC)
    admin.strategy.add_entry("cn=svc,dc=corp", {"userPassword": "svc-pw", "objectClass": "person", "sn": "svc"})
    admin.strategy.add_entry("cn=Ivan,ou=users,dc=corp", {
        "userPassword": "ivan-pw", "sAMAccountName": "ivan", "objectClass": "person", "sn": "Ivanov",
        "memberOf": ["CN=QA-Leads,OU=Groups,DC=corp", "CN=All,OU=Groups,DC=corp"]})
    monkeypatch.setattr(sso, "LDAP_STRATEGY", ldap3.MOCK_SYNC)
    monkeypatch.setattr(sso, "LDAP_SERVER", server)
    for k, v in {"TESTGEN_LDAP_URL": "ldaps://fake-dc", "TESTGEN_LDAP_BASE_DN": "dc=corp",
                 "TESTGEN_LDAP_BIND_DN": "cn=svc,dc=corp", "TESTGEN_LDAP_BIND_PASSWORD": "svc-pw"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "LOCAL_LOGIN", False)
    monkeypatch.setattr(auth, "ADMINS", set())
    yield
    auth.save_sso_settings({})


def test_ldap_login(directory, web):
    assert web.get("/api/auth/me").json()["sso"]["ldap"]
    for name, pw in (("ivan", "wrong"), ("ivan", ""), ("*", "ivan-pw"), ("nobody", "ivan-pw")):
        assert web.post("/api/auth/login", json={"username": name, "password": pw}).status_code == 401, name
    auth.save_sso_settings({"admin_groups": ["QA-Leads"]})
    r = web.post("/api/auth/login", json={"username": "Ivan", "password": "ivan-pw"})
    assert r.status_code == 200 and r.json()["user"] == "ivan"
    me = web.get("/api/auth/me").json()
    assert me["groups"] == ["All", "QA-Leads"] and me["is_admin"]
    # A directory user has no local password: turning LDAP off does not let anyone in with an empty one.
    assert not auth.verify("ivan", "")
