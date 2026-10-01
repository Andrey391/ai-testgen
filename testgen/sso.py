"""Corporate login (stage 5.2): OpenID Connect (Keycloak, ADFS, Blitz, Azure AD and other OIDC
providers) and LDAP / Active Directory. Groups of the directory become roles in projects
(access.py, auth.sso_settings()).

OIDC - authorization code flow with PKCE, the client is confidential:
    TESTGEN_OIDC_ISSUER          https://sso.company.ru/realms/qa   (/.well-known/openid-configuration)
    TESTGEN_OIDC_CLIENT_ID, TESTGEN_OIDC_CLIENT_SECRET
    TESTGEN_OIDC_SCOPES          default "openid profile email"
    TESTGEN_OIDC_USERNAME_CLAIM  default "preferred_username"
    TESTGEN_OIDC_GROUPS_CLAIM    default "groups" (Keycloak: the "Group Membership" mapper)
    TESTGEN_OIDC_TITLE           the login button, default "Корпоративный вход"
    TESTGEN_OIDC_REDIRECT_URL    default <studio>/api/auth/oidc/callback
    TESTGEN_OIDC_CACERT          CA bundle of the provider (e.g. the company's root)
The ID token's signature (JWKS of the provider), issuer, audience, expiry and nonce are checked.

LDAP - the login form of the studio; the service account finds the user, then the user's own
password is checked by a bind as that user:
    TESTGEN_LDAP_URL             ldaps://dc.company.ru:636 (or ldap:// with TESTGEN_LDAP_START_TLS=on)
    TESTGEN_LDAP_BIND_DN, TESTGEN_LDAP_BIND_PASSWORD   the service account (read only)
    TESTGEN_LDAP_BASE_DN         DC=company,DC=ru
    TESTGEN_LDAP_USER_FILTER     default (|(sAMAccountName={username})(uid={username}))
    TESTGEN_LDAP_GROUP_ATTR      default memberOf (group DNs; the CN is the group's name)
    TESTGEN_LDAP_CACERT          CA bundle for ldaps / StartTLS

With either one configured, self-registration and local passwords are off unless
TESTGEN_SIGNUP=on / TESTGEN_LOCAL_LOGIN=on (a break-glass admin, for instance).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

TRANSPORT: httpx.BaseTransport | None = None      # tests: a fake identity provider
LDAP_STRATEGY = None                               # tests: ldap3.MOCK_SYNC
LDAP_SERVER = None                                 # tests: the mock server with its entries
FLOW_TTL = 600
_discovery: dict[str, tuple[float, dict]] = {}


class SsoError(Exception):
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip() or default


# ---------- OIDC ----------

def oidc_enabled() -> bool:
    return bool(_env("TESTGEN_OIDC_ISSUER") and _env("TESTGEN_OIDC_CLIENT_ID"))


def oidc_title() -> str:
    return _env("TESTGEN_OIDC_TITLE", "Корпоративный вход") if oidc_enabled() else ""


def _http() -> httpx.Client:
    return httpx.Client(timeout=15, verify=_env("TESTGEN_OIDC_CACERT") or True, transport=TRANSPORT)


def discovery() -> dict:
    issuer = _env("TESTGEN_OIDC_ISSUER").rstrip("/")
    cached = _discovery.get(issuer)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    try:
        with _http() as h:
            r = h.get(issuer + "/.well-known/openid-configuration")
            r.raise_for_status()
            doc = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise SsoError(f"Провайдер входа {issuer} недоступен: {e}")
    _discovery[issuer] = (time.time(), doc)
    return doc


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def start(redirect_uri: str) -> tuple[str, dict]:
    """-> (the provider's authorization URL, the flow to keep in a signed cookie until the callback)."""
    d = discovery()
    flow = {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24),
            "verifier": secrets.token_urlsafe(48), "redirect_uri": redirect_uri, "exp": int(time.time()) + FLOW_TTL}
    query = {"response_type": "code", "client_id": _env("TESTGEN_OIDC_CLIENT_ID"), "redirect_uri": redirect_uri,
             "scope": _env("TESTGEN_OIDC_SCOPES", "openid profile email"), "state": flow["state"],
             "nonce": flow["nonce"], "code_challenge_method": "S256",
             "code_challenge": _b64(hashlib.sha256(flow["verifier"].encode()).digest())}
    return d["authorization_endpoint"] + ("&" if "?" in d["authorization_endpoint"] else "?") + urlencode(query), flow


def finish(flow: dict, code: str, state: str) -> tuple[str, list[str]]:
    """The callback: code -> tokens -> (username, groups). SsoError explains a refusal."""
    import jwt
    if not flow or flow.get("exp", 0) < time.time():
        raise SsoError("Вход устарел — начните заново")
    if not secrets.compare_digest(str(flow.get("state", "")), state or ""):
        raise SsoError("Неверный параметр state: вход начат не в этом браузере")
    d = discovery()
    client_id = _env("TESTGEN_OIDC_CLIENT_ID")
    try:
        with _http() as h:
            r = h.post(d["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": flow["redirect_uri"],
                "client_id": client_id, "client_secret": _env("TESTGEN_OIDC_CLIENT_SECRET"),
                "code_verifier": flow["verifier"]}, headers={"Accept": "application/json"})
            if r.status_code >= 400:
                raise SsoError(f"Провайдер входа отказал: {r.text[:200]}")
            tokens = r.json()
            jwks = h.get(d["jwks_uri"]).json()
            id_token = tokens.get("id_token")
            if not id_token:
                raise SsoError("Провайдер входа не вернул id_token (нужен scope openid)")
            header = jwt.get_unverified_header(id_token)
            key = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None) \
                or (jwks.get("keys") or [None])[0]
            if not key:
                raise SsoError("У провайдера входа нет ключей подписи (jwks_uri)")
            claims = jwt.decode(id_token, jwt.PyJWK(key).key, algorithms=[header.get("alg", "RS256")],
                                audience=client_id, issuer=d.get("issuer"), leeway=60,
                                options={"require": ["exp", "iat", "sub"]})
            if not secrets.compare_digest(str(claims.get("nonce", "")), flow["nonce"]):
                raise SsoError("Неверный nonce в id_token")
            groups_claim = _env("TESTGEN_OIDC_GROUPS_CLAIM", "groups")
            if groups_claim not in claims and tokens.get("access_token") and d.get("userinfo_endpoint"):
                info = h.get(d["userinfo_endpoint"], headers={"Authorization": f"Bearer {tokens['access_token']}"})
                if info.status_code == 200:
                    claims = info.json() | claims
    except httpx.HTTPError as e:
        raise SsoError(f"Провайдер входа недоступен: {e}")
    except jwt.PyJWTError as e:
        raise SsoError(f"id_token не прошёл проверку: {e}")
    username = str(claims.get(_env("TESTGEN_OIDC_USERNAME_CLAIM", "preferred_username")) or claims.get("email")
                   or "").strip()
    raw = claims.get(_env("TESTGEN_OIDC_GROUPS_CLAIM", "groups")) or []
    groups = [str(g).strip().lstrip("/") for g in (raw if isinstance(raw, list) else [raw]) if str(g).strip()]
    return username, groups


def pack_flow(flow: dict, sign) -> str:
    payload = _b64(json.dumps(flow).encode())
    return f"{payload}.{sign(payload)}"


def unpack_flow(cookie: str | None, sign) -> dict | None:
    try:
        payload, sig = (cookie or "").split(".")
        if not secrets.compare_digest(sig, sign(payload)):
            return None
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None


# ---------- LDAP / Active Directory ----------

def ldap_enabled() -> bool:
    return bool(_env("TESTGEN_LDAP_URL") and _env("TESTGEN_LDAP_BASE_DN"))


def _cn(dn: str) -> str:
    first = dn.split(",")[0].strip()
    return first.split("=", 1)[1] if first.lower().startswith("cn=") else dn


def ldap_login(username: str, password: str) -> tuple[str, list[str]] | None:
    """(username, groups) when the directory accepts the password; None otherwise."""
    import ldap3
    from ldap3.utils.conv import escape_filter_chars
    username = username.strip().lower()
    if not username or not password:     # an empty password is an anonymous bind, which "succeeds"
        return None
    kw = {"client_strategy": LDAP_STRATEGY} if LDAP_STRATEGY else {}
    server = LDAP_SERVER
    if server is None:
        tls = None
        if _env("TESTGEN_LDAP_CACERT"):
            import ssl
            tls = ldap3.Tls(ca_certs_file=_env("TESTGEN_LDAP_CACERT"), validate=ssl.CERT_REQUIRED)
        server = ldap3.Server(_env("TESTGEN_LDAP_URL"), get_info=ldap3.NONE, tls=tls, connect_timeout=10)
    start_tls = _env("TESTGEN_LDAP_START_TLS", "off").lower() in ("on", "1", "true", "yes")
    try:
        svc = ldap3.Connection(server, _env("TESTGEN_LDAP_BIND_DN") or None, _env("TESTGEN_LDAP_BIND_PASSWORD") or None,
                               read_only=True, receive_timeout=15, **kw)
        if start_tls:
            svc.open()
            svc.start_tls()
        if not svc.bind():
            raise SsoError("Служебная учётная запись LDAP не принята каталогом (TESTGEN_LDAP_BIND_DN)")
        attr = _env("TESTGEN_LDAP_GROUP_ATTR", "memberOf")
        flt = _env("TESTGEN_LDAP_USER_FILTER", "(|(sAMAccountName={username})(uid={username}))")
        svc.search(_env("TESTGEN_LDAP_BASE_DN"), flt.replace("{username}", escape_filter_chars(username)),
                   attributes=[attr])
        entries = list(svc.entries)
        svc.unbind()
        if len(entries) != 1:
            return None
        dn = entries[0].entry_dn
        values = entries[0].entry_attributes_as_dict.get(attr) or []
        user = ldap3.Connection(server, dn, password, read_only=True, receive_timeout=15, **kw)
        if start_tls:
            user.open()
            user.start_tls()
        ok = user.bind()
        user.unbind()
    except ldap3.core.exceptions.LDAPException as e:
        raise SsoError(f"Каталог LDAP недоступен: {e}")
    return (username, sorted({_cn(str(v)) for v in values})) if ok else None
