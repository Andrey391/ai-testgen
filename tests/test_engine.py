"""Engine gaps (stage 2): iframes and shadow DOM, the new actions, before/after data preparation,
modules, logging in once per suite, 2FA and e-mail codes, browsers / devices / locale. Every
feature is recorded, replayed by the runner and, where it applies, exported and run with pytest."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile

import pytest

from fakes import Resp, last_user_text, ref_for, text, tool
from helpers import arun, record
from stand import PASSWORD, TOTP_SECRET, USERNAME
from testgen import exporters, fs, mailbox, pipeline, projects, runner, runs, storage, suite
from testgen.agent import StudioSession
from testgen.runner import HealChoice
from testgen.steps import check_api, json_path, new_step
from testgen.testdata import totp

CREDS = {"username": USERNAME, "password": PASSWORD}
needs_pytest_playwright = pytest.mark.skipif(importlib.util.find_spec("pytest_playwright") is None,
                                             reason="pytest-playwright is not installed")


def _pytest(path, env_extra: dict, cwd) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TESTGEN_")} | env_extra
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(path)],
                          cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=300)


def _settings(project, **run):
    p = projects.get(project["id"])
    p["pipeline"]["run"].update(run)
    return projects.update(project["id"], {"pipeline": p["pipeline"]})


def _healer(fake_llm, name):
    fake_llm.script = lambda kind, kw: Resp(parsed=HealChoice(
        ref=ref_for(last_user_text(kw).split("Elements:")[1], name), reason="та же кнопка"))


# ---------- 2.1 iframes and shadow DOM ----------

def test_iframe_record_replay_heal_and_export(stand, project, fake_llm, tmp_path):
    async def script(r):
        await r.do("fill", "Номер карты", "4242 4242 4242 4242")
        await r.do("click", "Оплатить")
        await r.do("assert_element_text", "Платёж на проверке: **** 4242")
    steps = arun(record(f"{stand.url}/iframe.html", script))
    assert all(c.get("frame") == ["#pay"] for s in steps[1:] for c in s["locator"])
    test = {"id": "if1", "project_id": project["id"], "name": "Оплата в iframe", "url": f"{stand.url}/iframe.html",
            "steps": steps}
    rep = arun(runner.run_test(test))
    assert rep["passed"], rep["results"]

    stand.reset("v2")                     # the pay button inside the frame changed
    _healer(fake_llm, "Перейти к оплате")
    rep = arun(runner.run_test(test, cfg={"self_heal": True, "heal_mode": "review"}))
    assert rep["passed"], rep["results"]
    assert rep["proposals"][0]["new"][0]["frame"] == ["#pay"]

    stand.reset()
    code = exporters.to_playwright(test)
    assert "frame_locator('#pay')" in code
    compile(code, "t.py", "exec")
    if importlib.util.find_spec("pytest_playwright"):
        f = tmp_path / "test_iframe.py"
        f.write_text(code, "utf-8")
        r = _pytest(f, {"TESTGEN_BASE_URL": stand.url}, tmp_path)
        assert r.returncode == 0, r.stdout[-3000:]


def test_shadow_dom_record_replay_and_export(stand, tmp_path):
    async def script(r):
        await r.do("fill", "Шаг", "2")
        await r.do("click", "Плюс")
        await r.do("click", "Плюс")
        await r.do("assert_element_text", "Счётчик: 4")
    steps = arun(record(f"{stand.url}/shadow.html", script))
    assert any("tg-counter" in c.get("value", "") for s in steps for c in s["locator"] if c["kind"] == "css")
    test = {"id": "sh1", "project_id": "", "name": "Счётчик", "url": f"{stand.url}/shadow.html", "steps": steps}
    assert arun(runner.run_test(test))["passed"]
    if importlib.util.find_spec("pytest_playwright"):
        f = tmp_path / "test_shadow.py"
        f.write_text(exporters.to_playwright(test), "utf-8")
        r = _pytest(f, {"TESTGEN_BASE_URL": stand.url}, tmp_path)
        assert r.returncode == 0, r.stdout[-3000:]


def test_long_pages_are_listed_near_the_viewport_and_searchable(stand, project, fake_llm):
    """400 buttons: the listing stops at MAX_LISTED, find_elements finds the rest, and it is no step."""
    def script(kind, kw):
        last = last_user_text(kw)
        if "Found (refs" in last:
            return tool("click", ref=ref_for(last, "Товар 399"), description="Выбрать «Товар 399»")
        if "Выбран: Товар 399" in last:
            if "assert" not in json.dumps([m for m in kw["messages"] if m["role"] == "assistant"], default=str):
                return tool("assert_text_present", text="Выбран: Товар 399", description="Выбран товар 399")
            return tool("finish", status="passed", summary="ok")
        return tool("find_elements", text="Товар 399")
    fake_llm.script = script
    s = StudioSession(project, "Каталог", f"{stand.url}/many.html", "Выбрать товар 399")

    async def go():
        from test_agent_invariants import _drive
        try:
            await _drive(s)
            first_page = fake_llm.calls[0][1]["messages"][0]["content"][1]["text"]
            return first_page
        finally:
            await s.close()
    first_page = arun(go())
    assert "more elements are not listed" in first_page and '"Товар 399"' not in first_page
    assert s.status == "done", s.chat
    assert [x["action"] for x in s.steps] == ["navigate", "click", "assert_text_present"]


# ---------- 2.2 new actions ----------

def _actions_script():
    async def script(r):
        await r.do("upload_file", "Документ", "report.txt")
        await r.do("assert_text_present", value="Загружен: report.txt (12 байт)")
        await r.do("click", "Скачать отчёт")
        await r.do("assert_download", value=json.dumps({"name": "report*.csv", "min_bytes": 10}))
        await r.do("handle_dialog", value=json.dumps({"action": "accept", "expect": "Удалить черновик?"}))
        await r.do("click", "Удалить черновик")
        await r.do("assert_text_present", value="Черновик удалён")
        await r.do("handle_dialog", value=json.dumps({"action": "accept", "prompt_text": "Отчёт {{unique}}"}))
        await r.do("click", "Переименовать")
        await r.do("assert_text_present", value="Имя: Отчёт")
        await r.do("double_click", "Двойной клик")
        await r.do("assert_text_present", value="Сработал двойной клик")
        await r.do("drag_to", "Задача 1", target="done-zone")
        await r.do("assert_text_present", value="Готово: Задача 1")
        await r.do("click", "Открыть список в новой вкладке")
        await r.do("switch_tab", value="last")
        await r.do("assert_url_contains", value="list.html")
    return script


def test_new_actions_record_replay_and_export(stand, project, tmp_path):
    fs.write_bytes(projects.path(project["id"]) / "files" / "report.txt", b"hello world\n")
    steps = arun(record(f"{stand.url}/actions.html", _actions_script(), options={"project_id": project["id"]}))
    drag = next(s for s in steps if s["action"] == "drag_to")
    assert drag["target"] and drag["target"][0] == {"kind": "testid", "value": "done-zone"}
    test = {"id": "act1", "project_id": project["id"], "name": "Действия", "url": f"{stand.url}/actions.html",
            "steps": steps}
    rep = arun(runner.run_test(test))
    assert rep["passed"], [r for r in rep["results"] if r["status"] == "failed"]
    download = next(r for r in rep["results"] if "download" in (r.get("details") or {}))
    assert download["details"]["download"]["name"] == "report-2026.csv"

    # A dialog whose text is not the expected one fails the step.
    bad = [dict(s) for s in steps]
    k = next(i for i, s in enumerate(bad) if s["action"] == "handle_dialog")
    bad[k]["value"] = json.dumps({"action": "accept", "expect": "Совсем другое"})
    rep = arun(runner.run_test(test | {"steps": bad}))
    assert not rep["passed"] and "не содержит" in rep["results"][-1]["error"]

    code = exporters.to_playwright(test)
    for needle in ("set_input_files(FILES / 'report.txt')", "page.expect_download()", "_answer_dialog",
                   "page = _switch_tab(page, 'last')", ".dblclick()", ".drag_to("):
        assert needle in code, needle
    if importlib.util.find_spec("pytest_playwright"):
        (tmp_path / "fixtures").mkdir()
        (tmp_path / "fixtures" / "report.txt").write_bytes(b"hello world\n")
        f = tmp_path / "test_actions.py"
        f.write_text(code, "utf-8")
        r = _pytest(f, {"TESTGEN_BASE_URL": stand.url}, tmp_path)
        assert r.returncode == 0, r.stdout[-3000:]


# ---------- 2.4 before / after ----------

def _api(method, url, description, **spec):
    return new_step("api_request", description, json.dumps({"method": method, "url": url, **spec},
                                                           ensure_ascii=False), source="manual")


def _orders_test(project, stand, final="Заказ #{{vars.order_id}}: Заказ для теста"):
    return {"id": "ord1", "project_id": project["id"], "name": "Заказ виден", "url": f"{stand.url}/orders.html",
            "before": [_api("POST", "/api/orders", "Создать заказ", body={"title": "Заказ для теста"},
                            save={"order_id": "$.id"}, expect_status=201)],
            "steps": [new_step("navigate", "Открыть заказы", f"{stand.url}/orders.html"),
                      new_step("assert_text_present", "Заказ в списке", final)],
            "after": [_api("DELETE", "/api/orders/{{vars.order_id}}", "Удалить заказ")]}


def test_before_after_prepare_and_always_clean_up(stand, project, tmp_path):
    test = _orders_test(project, stand)
    rep = arun(runner.run_test(test, base_url=stand.url))
    assert rep["passed"], (rep["before"], rep["results"])
    assert rep["before"][0]["details"]["api"]["saved"] == ["order_id"] and stand.orders == []
    assert "Заказ для теста" not in json.dumps(rep["before"], ensure_ascii=False)     # bodies are not kept

    failing = _orders_test(project, stand, final="Нет такого заказа")
    rep = arun(runner.run_test(failing, base_url=stand.url))
    assert not rep["passed"] and rep["after"][0]["status"] == "passed" and stand.orders == []

    with pytest.raises(ValueError, match="только в блоке"):
        check_api(_api("DELETE", "/api/orders/{{vars.order_id}}", "x"), stand.url, "before")
    with pytest.raises(ValueError, match="созданного в этом прогоне"):
        check_api(_api("DELETE", "/api/orders/1", "x"), stand.url, "after")
    with pytest.raises(ValueError, match="только запросы к тестируемому"):
        check_api(_api("POST", "https://evil.example/api", "x"), stand.url, "before")
    assert json_path({"a": [{"b": 7}]}, "$.a[0].b") == 7

    if importlib.util.find_spec("pytest_playwright"):
        f = tmp_path / "test_orders.py"
        f.write_text(exporters.to_playwright(test), "utf-8")
        r = _pytest(f, {"TESTGEN_BASE_URL": stand.url}, tmp_path)
        assert r.returncode == 0, r.stdout[-3000:]
        assert stand.orders == []


# ---------- 2.5 modules ----------

def test_modules_run_heal_once_and_export(stand, project, save_test, fake_llm, tmp_path):
    async def add(r):
        await r.do("fill", "Что купить", "Кефир")
        await r.do("click", "Добавить")
    msteps = arun(record(f"{stand.url}/list.html", add))[1:]
    msteps[0]["value"] = "{{params.item}}"          # the module's parameter
    stand.reset()
    module = save_test("Добавить пункт", msteps, role="module")
    main = save_test("Покупка молока", [
        new_step("navigate", "Открыть список", f"{stand.url}/list.html"),
        new_step("use_module", "Добавить «Молоко»", json.dumps({"module": module["id"], "params": {"item": "Молоко"}})),
        new_step("assert_text_present", "В списке молоко", "Молоко"),
        new_step("assert_text_present", "Три пункта", "Всего: 3")])
    p = _settings(project, analyze_failures=False, retry_failed=False, trace="off")
    run = arun(pipeline.run_and_record(p, main))
    assert run["status"] == "passed", run["results"]
    assert [x["description"] for x in run["results"][1]["sub"]] == [s["description"] for s in msteps]

    stand.reset("v2")                     # the button changed: the module is healed, not the test
    _healer(fake_llm, "Добавить пункт")
    run = arun(pipeline.run_and_record(p, main))
    assert run["status"] == "passed" and run["proposals"] == 1
    assert storage.load(module["id"])["heal_proposals"][0]["module"] is True
    assert not storage.load(main["id"]).get("heal_proposals")

    stand.reset()
    files = exporters.bundle(p, [main], lookup=storage.load)
    code = files["tests/test_покупка_молока.py"]
    assert "def module_добавить_пункт(page, app_url, credentials, testdata, data, **params):" in code
    assert "params['item']" in code
    if importlib.util.find_spec("pytest_playwright"):
        for path, body in files.items():
            (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / path).write_text(body, "utf-8") if isinstance(body, str) else (tmp_path / path).write_bytes(body)
        r = _pytest(tmp_path, {"TESTGEN_BASE_URL": stand.url}, tmp_path)
        assert r.returncode == 0, r.stdout[-3000:]


# ---------- 2.3 log in once per suite ----------

def test_suite_logs_in_once_and_relogs_when_the_session_expired(stand, project, save_test, tmp_path):
    async def login(r):
        await r.do("fill", "Логин", "{{username}}")
        await r.do("fill", "Пароль", "{{password}}")
        await r.do("click", "Войти")
        await r.do("assert_element_text", "Всего: 2")
    lt = save_test("Вход", arun(record(f"{stand.url}/login.html", login, CREDS)), role="login")
    acc = save_test("Кабинет", [new_step("navigate", "Открыть кабинет", f"{stand.url}/account.html"),
                                new_step("assert_text_present", "Приветствие", "Здравствуйте, demo")])
    p = _settings(project, analyze_failures=False, retry_failed=False, self_heal=False, trace="always")
    stand.logins = 0
    s = suite.new(p, [lt, acc])
    s = arun(suite.run(p, s, [lt, acc]))
    assert s["passed"], s["items"]
    assert stand.logins == 2                    # once for the suite, once the login test itself
    state = pipeline.load_state(p["id"], CREDS)
    token = next(c["value"] for c in state["cookies"] if c["name"] == "auth")

    stand.expire_sessions()                     # the saved login stops working
    run = arun(pipeline.run_and_record(p, acc))
    assert run["status"] == "passed" and run.get("relogin"), run["results"]

    # The saved login is a secret: not in the trace, not in the export.
    blobs = []
    for r in (run,):
        f = runs.file(r, r["trace"]) if r["trace"] else None
        if f:
            with zipfile.ZipFile(f) as z:
                blobs.append(b"".join(z.read(n) for n in z.namelist()))
    new_token = next(c["value"] for c in pipeline.load_state(p["id"], CREDS)["cookies"] if c["name"] == "auth")
    assert blobs and all(new_token.encode() not in b and token.encode() not in b for b in blobs)
    files = exporters.bundle(p, [lt, acc], lookup=storage.load, login=lt)
    assert all(new_token not in (v if isinstance(v, str) else "") for v in files.values())
    assert "storage_state" in files["conftest.py"] and "def _login(" in files["conftest.py"]
    if importlib.util.find_spec("pytest_playwright"):
        for path, body in files.items():
            (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / path).write_text(body, "utf-8")
        r = _pytest(tmp_path, {"TESTGEN_BASE_URL": stand.url, "TESTGEN_USERNAME": USERNAME,
                               "TESTGEN_PASSWORD": PASSWORD}, tmp_path)
        assert r.returncode == 0 and "2 passed" in r.stdout, r.stdout[-3000:]


# ---------- 2.6 2FA and codes from e-mails ----------

def test_totp_and_email_codes(stand, project):
    projects.set_app_credentials(project["id"], USERNAME, PASSWORD, totp_secret=TOTP_SECRET)
    otp = {"id": "otp1", "project_id": project["id"], "name": "Вход с 2FA", "url": f"{stand.url}/login-2fa.html",
           "steps": arun(record(f"{stand.url}/login-2fa.html", _two_factor(), storage.credentials(
               {"id": "x", "project_id": project["id"]})))}
    assert any(s["value"] == "{{totp}}" for s in otp["steps"])
    rep = arun(runner.run_test(otp, credentials=storage.credentials(otp), cfg={"trace": "always"},
                               run_dir=None))
    assert rep["passed"], rep["results"]
    assert TOTP_SECRET not in json.dumps(rep, ensure_ascii=False) + json.dumps(otp, ensure_ascii=False)
    assert "credentials['totp']" in exporters.to_playwright(otp) and "def totp(" in exporters.to_playwright(otp)

    mailbox.set_mailbox(project["id"], {"kind": "mailpit", "url": f"{stand.url}/mailpit"})

    async def email(r):
        await r.do("fill", "Email", "{{faker.email}}")
        await r.do("click", "Отправить код")
        await r.do("read_email", value=json.dumps({"to": "{{faker.email}}", "save": "code"}))
        await r.do("fill", "Код из письма", "{{vars.code}}")
        await r.do("click", "Подтвердить почту")
        await r.do("assert_text_present", value="Почта подтверждена")
    steps = arun(record(f"{stand.url}/confirm-email.html", email, options={"project_id": project["id"]}))
    rep = arun(runner.run_test({"id": "em1", "project_id": project["id"], "name": "Почта",
                                "url": f"{stand.url}/confirm-email.html", "steps": steps}))
    assert rep["passed"], rep["results"]
    assert "_read_email(" in exporters.to_playwright({"name": "Почта", "url": stand.url, "steps": steps})


def _two_factor():
    async def script(r):
        await r.do("fill", "Логин", "{{username}}")
        await r.do("fill", "Пароль", "{{password}}")
        await r.do("click", "Далее")
        await r.do("fill", "Код из приложения", "{{totp}}")
        await r.do("click", "Подтвердить")
        await r.do("assert_text_present", value="Вход подтверждён")
    return script


def test_totp_matches_rfc6238():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"          # "12345678901234567890"
    assert totp(secret, at=59) == "287082" and totp(secret, at=1111111109) == "081804"


# ---------- 2.7 browsers, devices, locale ----------

def _installed(engine: str) -> bool:
    """The engine is installed and starts on this machine (CI installs all three)."""
    async def probe():
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        try:
            b = await getattr(pw, engine).launch()
            await b.close()
            return True
        except Exception:
            return False
        finally:
            await pw.stop()
    return arun(probe())


def test_browsers_devices_and_locale(stand, project, save_test):
    engines = [e for e in ("chromium", "firefox", "webkit") if _installed(e)]
    p = _settings(project, analyze_failures=False, retry_failed=False, self_heal=False, trace="off",
                  browsers=engines, devices=["desktop", "iPhone 13"], locale="ru-RU", timezone="Europe/Moscow")
    t = save_test("Локаль", [new_step("navigate", "Открыть", f"{stand.url}/locale.html"),
                             new_step("assert_text_present", "Язык", "Язык: ru-RU"),
                             new_step("assert_text_present", "Пояс", "Пояс: Europe/Moscow")])
    m = save_test("Мобильный", [new_step("navigate", "Открыть", f"{stand.url}/locale.html"),
                                new_step("assert_text_present", "Мобильный", "Мобильный")])
    s = suite.new(p, [t])
    assert len(s["items"]) == 2 * len(engines)
    s = arun(suite.run(p, s, [t]))
    assert s["passed"], [(i["name"], i["error"]) for i in s["items"] if i["status"] != "passed"]
    run = arun(pipeline.run_and_record(p, m, engine="chromium", device="iPhone 13"))
    assert run["status"] == "passed" and run["device"] == "iPhone 13", run["results"]
