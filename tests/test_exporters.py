"""Exporters: code shape, no secrets, and exported tests actually running on the stand."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

import pytest

from helpers import arun, record
from stand import PASSWORD, USERNAME
from testgen import exporters
from testgen.steps import new_step

CREDS = {"username": USERNAME, "password": PASSWORD}
needs_pytest_playwright = pytest.mark.skipif(importlib.util.find_spec("pytest_playwright") is None,
                                             reason="pytest-playwright is not installed")


def _pytest(path, env_extra: dict, cwd) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TESTGEN_")} | env_extra
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(path)],
                          cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=300)


def _login_test(stand, traffic=None) -> dict:
    async def script(r):
        await r.do("fill", "Логин", "{{username}}", description="Ввести логин")
        await r.do("fill", "Пароль", "{{password}}", description="Ввести пароль")
        await r.do("click", "Войти", description="Нажать «Войти»")
        await r.do("assert_element_text", "Всего: 2", description="В списке два пункта")
    steps = arun(record(f"{stand.url}/login.html", script, CREDS, traffic))
    return {"id": "login", "name": "Вход в систему", "url": f"{stand.url}/login.html", "scenario": "Вход",
            "steps": steps}


def test_locators_become_or_chains_and_counts_do_not():
    loc = [{"kind": "testid", "value": "a"}, {"kind": "role", "role": "button", "name": "Go"},
           {"kind": "css", "value": "#a"}, {"kind": "text", "value": "Go"}, {"kind": "css", "value": "div > a"}]
    t = {"name": "x", "url": "https://app.test/", "steps": [
        new_step("click", "c", locator=loc), new_step("assert_count", "n", "3", locator=loc[:2])]}
    code = exporters.to_playwright(t)
    compile(code, "x.py", "exec")
    assert code.count(".or_(") == exporters.MAX_ALTERNATIVES - 1        # at most 4 candidates
    assert "div > a" not in code
    assert "expect(element).to_have_count(3)" in code
    assert "page.get_by_test_id('a')\n" in code                          # the count uses one candidate


def test_export_holds_no_secrets(stand):
    t = _login_test(stand)
    code = exporters.to_playwright(t)
    assert PASSWORD not in code and "credentials['password']" in code
    assert "app_url + '/login.html'" in code
    assert PASSWORD not in exporters.to_gherkin(t)


@needs_pytest_playwright
def test_exported_test_survives_a_layout_change(stand, tmp_path, monkeypatch):
    t = _login_test(stand)
    f = tmp_path / "test_login.py"
    f.write_text(exporters.to_playwright(t), "utf-8")
    env = {"TESTGEN_BASE_URL": stand.url, "TESTGEN_USERNAME": USERNAME, "TESTGEN_PASSWORD": PASSWORD}
    r = _pytest(f, env, tmp_path)
    assert r.returncode == 0, r.stdout[-3000:]

    stand.reset("v2")          # new layout: the login button lost its test id and id
    r = _pytest(f, env, tmp_path)
    assert r.returncode == 0, r.stdout[-3000:]

    # The same export with only the first locator candidate breaks on v2: the fallbacks did it.
    monkeypatch.setattr(exporters, "MAX_ALTERNATIVES", 1)
    f1 = tmp_path / "test_login_first_only.py"
    f1.write_text(exporters.to_playwright(t), "utf-8")
    r = _pytest(f1, env, tmp_path)
    assert r.returncode != 0


@needs_pytest_playwright
def test_bundle_with_conftest_and_testdata(stand, tmp_path):
    async def form(r):
        await r.do("fill", "Email", "{{faker.email}}")
        await r.do("fill", "Имя", "{{faker.first_name}}")
        await r.do("click", "Согласен с правилами")
        await r.do("click", "Зарегистрироваться")
        await r.do("assert_text_present", value="{{faker.email}}")
        await r.do("assert_no_console_errors")
    reg = {"id": "reg", "name": "Регистрация", "url": f"{stand.url}/form.html", "scenario": "",
           "steps": arun(record(f"{stand.url}/form.html", form))}
    files = exporters.bundle({"name": "Стенд", "base_url": stand.url}, [_login_test(stand), reg])
    assert {"conftest.py", "pytest.ini", "requirements.txt", "README.md"} <= set(files)
    assert "def testdata" in files["conftest.py"] and "def testdata" not in "".join(
        v for k, v in files.items() if k.startswith("tests/"))
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text, "utf-8")
    r = _pytest(tmp_path, {"TESTGEN_BASE_URL": stand.url, "TESTGEN_USERNAME": USERNAME,
                           "TESTGEN_PASSWORD": PASSWORD}, tmp_path)
    assert r.returncode == 0 and "2 passed" in r.stdout, r.stdout[-3000:]


def test_api_tests_from_recorded_traffic(stand, tmp_path):
    traffic: list = []
    t = _login_test(stand, traffic)
    assert any(e["url"].endswith("/api/login") for e in traffic)
    login = next(e for e in traffic if e["url"].endswith("/api/login"))
    assert PASSWORD not in json.dumps(traffic) and "{{password}}" in login["post_data"]
    code = exporters.to_api_tests(t, traffic)
    compile(code, "api.py", "exec")
    assert PASSWORD not in code and "env('TESTGEN_PASSWORD')" in code
    f = tmp_path / "test_api.py"
    f.write_text(code, "utf-8")
    stand.reset()
    r = _pytest(f, {"TESTGEN_USERNAME": USERNAME, "TESTGEN_PASSWORD": PASSWORD}, tmp_path)
    assert r.returncode == 0 and "2 passed" in r.stdout, r.stdout[-3000:]    # POST /api/login, GET /api/items
