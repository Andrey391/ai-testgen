"""Planner (site exploration, coverage) and suite runs (parallel, quarantine, JUnit, Allure, CLI)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

from conftest import ROOT
from helpers import arun
from testgen import explorer, pipeline, projects, reports, storage, suite
from testgen.steps import new_step


def test_explore_maps_pages_and_skips_dangerous_links(stand, project):
    m = arun(explorer.explore(project, stand.url))
    assert m["status"] == "done", m
    paths = {p["url"].split(stand.url)[1] for p in m["pages"]}
    assert {"/", "/login.html", "/form.html", "/list.html", "/errors.html"} <= paths
    assert not stand.dangerous                                  # /logout and /delete-account never opened
    assert not any("example.org" in p["url"] for p in m["pages"])
    assert m["skipped"] >= 3
    req = explorer.to_requirements(m)
    assert "Email (email, обязательное)" in req and "Страна (select" in req and "Зарегистрироваться" in req
    assert explorer.latest(project["id"])["id"] == m["id"]


def test_coverage_by_screens(stand, project, save_test):
    arun(explorer.explore(project, stand.url))
    save_test("Форма", [new_step("navigate", "open", f"{stand.url}/form.html")])
    cov = explorer.coverage(project["id"])
    by_path = {p["path"]: p for p in cov["pages"]}
    assert by_path["/form.html"]["tests"] == ["Форма"] and not by_path["/list.html"]["tests"]
    assert cov["covered"] == 1 and cov["total"] >= 5


def test_explore_behind_login(stand, project, save_test):
    from helpers import record
    from stand import PASSWORD, USERNAME

    async def script(r):
        await r.do("fill", "Логин", "{{username}}")
        await r.do("fill", "Пароль", "{{password}}")
        await r.do("click", "Войти")
        await r.do("assert_element_text", "Всего: 2")
    save_test("Вход", arun(record(f"{stand.url}/login.html", script, {"username": USERNAME, "password": PASSWORD})))
    p = projects.get(project["id"])
    p["pipeline"]["explore"].update(login_test="Вход", max_pages=2)
    p = projects.update(p["id"], {"pipeline": p["pipeline"]})
    m = arun(explorer.explore(p, stand.url))
    assert m["status"] == "done" and len(m["pages"]) == 2
    assert ("POST", "/api/login") in stand.requests


def _suite_project(stand, project, save_test):
    ok = save_test("Список открывается", [new_step("navigate", "open", f"{stand.url}/list.html"),
                                          new_step("assert_text_present", "Всего", "Всего: 2")], tags=["smoke"])
    bad = save_test("Сломанный", [new_step("navigate", "open", f"{stand.url}/list.html"),
                                  new_step("assert_text_present", "Нет такого", "Нет такого текста")],
                    tags=["smoke"], quarantine={"on": True, "reason": "известная проблема"})
    other = save_test("Без тега", [new_step("navigate", "open", f"{stand.url}/form.html")])
    p = projects.get(project["id"])
    p["pipeline"]["run"].update(analyze_failures=False, self_heal=False, retry_failed=False, trace="off")
    return projects.update(p["id"], {"pipeline": p["pipeline"]}), ok, bad, other


def test_suite_run_quarantine_and_reports(stand, project, save_test, tmp_path):
    p, ok, bad, other = _suite_project(stand, project, save_test)
    tests = storage.select(p["id"], tags=["smoke"])
    assert {t["id"] for t in tests} == {ok["id"], bad["id"]}
    s = suite.new(p, tests, tags=["smoke"])
    s = arun(suite.run(p, s, tests, parallel=2))
    items = {i["name"]: i for i in s["items"]}
    assert items["Список открывается"]["status"] == "passed" and items["Сломанный"]["status"] == "failed"
    assert s["passed"] and s["summary"]["quarantined_failed"] == 1       # quarantine does not fail the suite

    root = ET.fromstring(reports.junit(s))
    assert root.get("tests") == "2" and root.get("failures") == "0" and root.get("skipped") == "1"
    skipped = root.find(".//testcase[@name='Сломанный']/skipped")
    assert skipped is not None and "карантин" in skipped.get("message")

    reports.allure(s, tmp_path / "allure")
    results = [json.loads(f.read_text("utf-8")) for f in (tmp_path / "allure").glob("*-result.json")]
    assert sorted(r["status"] for r in results) == ["passed", "skipped"]
    assert suite.list_suites(p["id"])[0]["id"] == s["id"]


def test_cli_exit_codes_and_junit(stand, project, save_test, tmp_path):
    p, ok, bad, other = _suite_project(stand, project, save_test)
    env = os.environ | {"PYTHONIOENCODING": "utf-8"}
    cli = [sys.executable, "-m", "testgen.run", "--project", p["name"]]
    r = subprocess.run(cli + ["--tag", "smoke", "--junit", str(tmp_path / "j.xml")], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr                        # the failure is in quarantine
    assert (tmp_path / "j.xml").exists() and "PASS" in r.stdout

    storage.update(bad["id"], lambda t: t.update(quarantine={"on": False}))
    r = subprocess.run(cli + ["--tag", "smoke"], cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding="utf-8", timeout=300)
    assert r.returncode == 1 and "FAIL" in r.stdout

    r = subprocess.run(cli + ["--test", "Без тега", "--list"], cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    assert r.returncode == 0 and "Без тега" in r.stdout and "Сломанный" not in r.stdout
    r = subprocess.run([sys.executable, "-m", "testgen.run", "--project", "нет такого"], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 2


def test_pipeline_job_explores_when_there_is_no_spec(stand, project, fake_llm):
    """Planner as a source: the map goes to scenario design (the fake designs nothing, which is enough here)."""
    from fakes import Resp
    from testgen.scenarios import ScenarioPlan

    seen = {}

    def script(kind, kw):
        seen["context"] = kw["system"][1]["text"]
        return Resp(parsed=ScenarioPlan(feature="Стенд", assumptions=[], scenarios=[]))
    fake_llm.script = script
    job = pipeline.Job(project, [], "", stand.url, {}, explore=True)
    arun(job.run())
    assert job.status == "done", job.error
    assert job.requirements[0]["source"] == "Planner"
    assert "Карта приложения" in seen["context"] and "/form.html" in seen["context"]
