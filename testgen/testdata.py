"""Unique test data for forms: registration and similar flows break on a second
run if the e-mail or login is a literal ("user already exists").

Steps may hold placeholders that get a fresh value on every run:

    {{unique}}          a unique number, e.g. 172746391205
    {{today}}           today's date, YYYY-MM-DD
    {{faker.email}}     a unique address at example.com
    {{faker.name}}, {{faker.first_name}}, {{faker.last_name}}, {{faker.phone}},
    {{faker.company}}, {{faker.city}}, {{faker.address}}, {{faker.postcode}},
    {{faker.user_name}}, {{faker.word}}, {{faker.sentence}}

A value is generated once per run: the same {{faker.email}} in the "register"
step and the "log in" step is the same address. Exported tests carry the same
generator (exporters.py embeds the source of `DataValues` and `generate`), so
they behave like the studio's own runs. TESTGEN_FAKER_LOCALE picks the Faker
locale (en_US by default).

Besides test data, BrowserSession.expand substitutes:

    {{username}}, {{password}}   the login of the application (never shown to the model)
    {{totp}}                     the current one-time code of the login's TOTP secret (2FA)
    {{auth.name}}                an extra login parameter of the account (an OTP code of the
                                 test stand, a tenant, a PIN...): name - value pairs
    {{vars.name}}                a value saved in this run: from the response of a `before`
                                 request (api_request "save") or a code read from an e-mail
    {{params.name}}              a parameter of a module (a test used as a step, use_module)
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import os
import random
import re
import struct
import time

FIELDS = {"email", "name", "first_name", "last_name", "phone", "company", "city", "address", "postcode",
          "user_name", "word", "sentence"}
# Credentials, test data, run variables and module parameters: everything BrowserSession.expand substitutes.
PLACEHOLDER = re.compile(r"\{\{(username|password|totp|unique|today|faker\.[a-z_]+|vars\.[A-Za-z_]\w*"
                         r"|params\.[A-Za-z_]\w*|auth\.[A-Za-z_][A-Za-z0-9_]*)\}\}")
CREDENTIALS = ("username", "password", "totp")
PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def is_credential(key: str) -> bool:
    """A placeholder the account fills: login, password, one-time code, an extra login parameter."""
    return key in CREDENTIALS or key.startswith("auth.")


def env_name(key: str) -> str:
    """Environment variable of a credential placeholder in exported code: TESTGEN_PASSWORD, TESTGEN_AUTH_OTP."""
    return "TESTGEN_" + re.sub(r"\W", "_", key).upper()


def auth_params(credentials: dict) -> dict[str, str]:
    """Extra login parameters of an account: name -> value."""
    return {p["name"]: p["value"] for p in credentials.get("params") or [] if p.get("name") and p.get("value")}


def secret_pairs(credentials: dict) -> list[tuple[str, str]]:
    """(secret value, its placeholder): the password, secret login parameters, the TOTP secret.
    These never reach the model, steps, traces or recorded traffic."""
    out = [(credentials["password"], "{{password}}")] if credentials.get("password") else []
    out += [(p["value"], f"{{{{auth.{p['name']}}}}}") for p in credentials.get("params") or []
            if p.get("secret") and p.get("name") and p.get("value")]
    if credentials.get("totp_secret"):
        out.append((credentials["totp_secret"], "***"))
    # Longer first: a secret containing another one is replaced whole.
    return sorted(out, key=lambda x: -len(x[0]))


def secret_values(credentials: dict) -> list[str]:
    return [v for v, _ in secret_pairs(credentials)]


def keys(value: str) -> list[str]:
    """Test data placeholders used in a value (credentials, variables and parameters not included)."""
    return [k for k in PLACEHOLDER.findall(value or "") if not is_credential(k)
            and not k.startswith(("vars.", "params."))]


class DataValues(dict):
    """Placeholder -> value, generated on first use and then kept for the whole run."""

    def __missing__(self, key):
        value = generate(key, self)
        self[key] = value
        return value


def generate(key, known):
    if key == "unique":
        return f"{int(time.time() * 1000) % 10**10}{random.randint(10, 99)}"
    if key == "today":
        return datetime.date.today().isoformat()
    field = key[len("faker."):] if key.startswith("faker.") else ""
    if field not in FIELDS:
        raise ValueError(f"Unknown test data placeholder {{{{{key}}}}}")
    from faker import Faker
    fake = Faker(os.environ.get("TESTGEN_FAKER_LOCALE", "en_US"))
    if field == "email":
        return f"{fake.user_name()}.{known['unique']}@example.com"
    if field == "phone":
        return fake.phone_number()
    if field == "address":
        return fake.street_address()
    return str(getattr(fake, field)())


def totp(secret, at=None, digits=6, period=30):
    """RFC 6238 one-time code (SHA-1) of a base32 secret, as authenticator apps show it."""
    key = base64.b32decode(re.sub(r"[\s-]", "", str(secret)).upper() + "=" * (-len(re.sub(r"[\s-]", "", str(secret))) % 8))
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits
    return str(code).zfill(digits)
