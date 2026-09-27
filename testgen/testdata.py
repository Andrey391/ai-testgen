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
"""
from __future__ import annotations

import datetime
import os
import random
import re
import time

FIELDS = {"email", "name", "first_name", "last_name", "phone", "company", "city", "address", "postcode",
          "user_name", "word", "sentence"}
# Credentials plus test data: everything BrowserSession.expand substitutes.
PLACEHOLDER = re.compile(r"\{\{(username|password|unique|today|faker\.[a-z_]+)\}\}")
CREDENTIALS = ("username", "password")


def keys(value: str) -> list[str]:
    """Test data placeholders used in a value (credentials not included)."""
    return [k for k in PLACEHOLDER.findall(value or "") if k not in CREDENTIALS]


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
