"""Shared fixtures of the studio's own tests.

The data and secrets folders are moved to a temporary directory before testgen is
imported, so tests never touch the real data/ and secrets/. The LLM is replaced by
tests/fakes.FakeClient (no API calls); the application under test is tests/stand.py.

Tests that use the stand are marked `browser` automatically: `-m "not browser"` is
the fast unit part, `-m browser -n 4` (pytest-xdist) the browser part. Every xdist
worker is its own process with its own temporary data folder and stand.

The same tests on the shared storage of stage 5.5 (fs.py): TESTGEN_TEST_DB is the URL of a
PostgreSQL server - every xdist worker gets a database of its own there; TESTGEN_TEST_S3=moto puts
binary files into a local S3 (moto). Tests of the shared storage itself (fresh_database) are skipped
without TESTGEN_TEST_DB.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="testgen-tests-"))
os.environ["TESTGEN_DATA_DIR"] = str(_TMP / "data")
os.environ["TESTGEN_SECRETS_DIR"] = str(_TMP / "secrets")
for var in ("TESTGEN_USERNAME", "TESTGEN_PASSWORD", "TESTGEN_TOTP_SECRET", "TESTGEN_PROMPT_CACHE", "TESTGEN_BASE_URL",
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "TESTGEN_USD_RUB",
            "TESTGEN_OFFLINE", "TESTGEN_DATABASE_URL", "TESTGEN_S3_BUCKET", "TESTGEN_SECRET_KEY", "TESTGEN_VAULT_ADDR",
            "TESTGEN_OIDC_ISSUER", "TESTGEN_LDAP_URL", "TESTGEN_INSTANCE_URL", "TESTGEN_AUDIT_SYSLOG",
            "TESTGEN_AUDIT_FILE"):
    os.environ.pop(var, None)
_DB = os.environ.get("TESTGEN_TEST_DB", "").strip()
if _DB and not _DB.startswith("postgresql"):
    raise SystemExit("TESTGEN_TEST_DB — адрес PostgreSQL (postgresql+psycopg://…)")


def _admin_sql(sql: str) -> None:
    from sqlalchemy import create_engine, text
    admin = create_engine(_DB, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(sql))
    finally:
        admin.dispose()


def _create_database(prefix: str) -> str:
    """A new empty database on the TESTGEN_TEST_DB server -> its URL."""
    from sqlalchemy.engine import make_url
    name = f"{prefix}_{os.environ.get('PYTEST_XDIST_WORKER', 'main')}_{uuid.uuid4().hex[:6]}"
    _admin_sql(f'CREATE DATABASE "{name}"')
    return make_url(_DB).set(database=name).render_as_string(hide_password=False)


if _DB:
    # A database of its own for every xdist worker, as each has its own data folder.
    os.environ["TESTGEN_DATABASE_URL"] = _create_database("testgen")
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
from testgen import llm, projects, storage  # noqa: E402


def pytest_collection_modifyitems(items):
    for item in items:
        if {"stand", "_stand"} & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.browser)


@pytest.fixture
def fresh_database(monkeypatch):
    """An empty database of its own for one test (on the TESTGEN_TEST_DB server), dropped after it."""
    if not _DB:
        pytest.skip("needs TESTGEN_TEST_DB (PostgreSQL)")
    from sqlalchemy.engine import make_url
    from testgen import db
    url = _create_database("testgen_t")
    monkeypatch.setenv("TESTGEN_DATABASE_URL", url)
    yield url
    if db._engine and db._engine[0] == url:
        db._engine[1].dispose()
        db._engine = None
    _admin_sql(f'DROP DATABASE IF EXISTS "{make_url(url).database}" WITH (FORCE)')


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
