"""Shared fixtures of the studio's own tests.

The data lives in PostgreSQL, as in the studio: every xdist worker (each its own process) gets a
database of its own, created before testgen is imported and dropped at the end. The server is
TESTGEN_TEST_DB (default: postgresql+psycopg://testgen:testgen@127.0.0.1:5432/testgen - a role that
may create databases); TESTGEN_TEST_S3=moto puts binary files into a local S3 (moto). The local
cache goes to a temporary folder. The LLM is replaced by tests/fakes.FakeClient (no API calls); the
application under test is tests/stand.py.

Tests that use the stand are marked `browser` automatically: `-m "not browser"` is the fast unit
part, `-m browser -n 4` (pytest-xdist) the browser part.
"""
from __future__ import annotations

import atexit
import os
import sys
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

_TMP = Path(tempfile.mkdtemp(prefix="testgen-tests-"))
os.environ["TESTGEN_CACHE_DIR"] = str(_TMP / "cache")
for var in ("TESTGEN_USERNAME", "TESTGEN_PASSWORD", "TESTGEN_TOTP_SECRET", "TESTGEN_PROMPT_CACHE", "TESTGEN_BASE_URL",
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "TESTGEN_USD_RUB",
            "TESTGEN_OFFLINE", "TESTGEN_DATABASE_URL", "TESTGEN_S3_BUCKET", "TESTGEN_SECRET_KEY", "TESTGEN_VAULT_ADDR",
            "TESTGEN_OIDC_ISSUER", "TESTGEN_LDAP_URL", "TESTGEN_INSTANCE_URL", "TESTGEN_AUDIT_SYSLOG",
            "TESTGEN_AUDIT_FILE"):
    os.environ.pop(var, None)
SERVER = os.environ.get("TESTGEN_TEST_DB", "").strip() or "postgresql+psycopg://testgen:testgen@127.0.0.1:5432/testgen"


def _admin(sql: str) -> None:
    e = create_engine(SERVER, isolation_level="AUTOCOMMIT")
    try:
        with e.connect() as c:
            c.execute(text(sql))
    finally:
        e.dispose()


def new_database(prefix: str = "testgen") -> str:
    """A fresh database on the test server. -> its URL (drop_database() when done)."""
    name = f"{prefix}_{os.environ.get('PYTEST_XDIST_WORKER', 'main')}_{uuid.uuid4().hex[:8]}"
    _admin(f'CREATE DATABASE "{name}"')
    return make_url(SERVER).set(database=name).render_as_string(hide_password=False)


def drop_database(url: str) -> None:
    from testgen import db
    if db._engine and db._engine[0] == url:
        db._engine[1].dispose()
    try:
        _admin(f'DROP DATABASE IF EXISTS "{make_url(url).database}" WITH (FORCE)')
    except Exception as err:
        print(f"conftest: {make_url(url).database} not dropped: {err}", file=sys.stderr)


os.environ["TESTGEN_DATABASE_URL"] = new_database()
atexit.register(drop_database, os.environ["TESTGEN_DATABASE_URL"])
os.environ["TESTGEN_SECRET_KEY"] = "the studio's own tests: a long enough key"
_S3 = None
if os.environ.get("TESTGEN_TEST_S3") == "moto":
    from moto.server import ThreadedMotoServer
    _S3 = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    _S3.start()
    os.environ.update(TESTGEN_S3_ENDPOINT="http://%s:%d" % _S3.get_host_and_port(), TESTGEN_S3_BUCKET="testgen",
                      TESTGEN_S3_ACCESS_KEY="test", TESTGEN_S3_SECRET_KEY="test")
    import boto3
    boto3.client("s3", endpoint_url=os.environ["TESTGEN_S3_ENDPOINT"], aws_access_key_id="test",
                 aws_secret_access_key="test", region_name="us-east-1").create_bucket(Bucket="testgen")
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]

import pytest  # noqa: E402

from fakes import FakeClient  # noqa: E402
from stand import PASSWORD, USERNAME, Stand  # noqa: E402
from testgen import llm, projects, storage, vault  # noqa: E402

vault.LOCAL_KEY = _TMP / "secret.key"       # tests never make the key of local development of this computer


def pytest_collection_modifyitems(items):
    for item in items:
        if {"stand", "_stand"} & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.browser)


@pytest.fixture(scope="session")
def _stand():
    s = Stand()
    yield s
    s.close()


@pytest.fixture
def stand(_stand):
    _stand.reset()
    return _stand


MODEL = "test-model"


@pytest.fixture
def fake_llm(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(llm, "make_client", lambda api_key, base_url: client)
    monkeypatch.setattr(llm, "_clients", {})
    return client


@pytest.fixture
def project(stand):
    p = projects.create(f"Стенд {uuid.uuid4().hex[:6]}", base_url=stand.url)
    projects.set_app_credentials(p["id"], USERNAME, PASSWORD)
    projects.update_llm(p["id"], {"model": MODEL, "effort": "medium", "prices": {MODEL: [5, 25]}})
    return projects.get(p["id"])


@pytest.fixture
def save_test(project):
    def make(name: str, steps: list[dict], url: str = "", **extra) -> dict:
        return storage.save({"project_id": project["id"], "name": name, "url": url or project["base_url"],
                             "scenario": name, "steps": steps, **extra})
    return make
