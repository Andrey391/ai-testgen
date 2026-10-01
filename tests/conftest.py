"""Shared fixtures of the studio's own tests.

The data and secrets folders are moved to a temporary directory before testgen is
imported, so tests never touch the real data/ and secrets/. The LLM is replaced by
tests/fakes.FakeClient (no API calls); the application under test is tests/stand.py.
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
for var in ("TESTGEN_USERNAME", "TESTGEN_PASSWORD", "TESTGEN_PROMPT_CACHE", "TESTGEN_BASE_URL",
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
    os.environ.pop(var, None)
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]

import pytest  # noqa: E402

from fakes import FakeClient  # noqa: E402
from stand import PASSWORD, USERNAME, Stand  # noqa: E402
from testgen import llm, projects, storage  # noqa: E402


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
