"""Shared fixtures of the studio's own tests.

The data and secrets folders are moved to a temporary directory before testgen is
imported, so tests never touch the real data/ and secrets/. The LLM is replaced by
tests/fakes.FakeClient (no API calls); the application under test is tests/stand.py.

Tests that use the stand are marked `browser` automatically: `-m "not browser"` is
the fast unit part, `-m browser -n 4` (pytest-xdist) the browser part. Every xdist
worker is its own process with its own temporary data folder and stand.

The same tests on the shared storage of stage 5.5 (fs.py): TESTGEN_TEST_DB=sqlite keeps the data
in a SQLite database of the temporary folder (any other value is taken as the database URL, e.g.
a PostgreSQL one), TESTGEN_TEST_S3=moto puts binary files into a local S3 (moto).
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
if _DB == "sqlite":
    _DB = f"sqlite:///{(_TMP / 'testgen.db').as_posix()}"
elif _DB.startswith("postgresql"):
    # A database of its own for every xdist worker, as each has its own data folder.
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    _name = f"testgen_{os.environ.get('PYTEST_XDIST_WORKER', 'main')}_{uuid.uuid4().hex[:6]}"
    _admin = create_engine(_DB, isolation_level="AUTOCOMMIT")
    with _admin.connect() as _c:
        _c.execute(text(f'CREATE DATABASE "{_name}"'))
    _admin.dispose()
    _DB = make_url(_DB).set(database=_name).render_as_string(hide_password=False)
if _DB:
    os.environ["TESTGEN_DATABASE_URL"] = _DB
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
