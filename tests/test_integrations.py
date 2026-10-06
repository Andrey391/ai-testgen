"""Stage 3 integrations: Test IT (REST) end to end through the pipeline, Allure TestOps preset and
its destructive tools, trackers (YouTrack, Yandex Tracker, Kaiten) as requirement sources and
defect targets, specification files (.docx, .pdf), notifications (Telegram, Mattermost, e-mail)."""
from __future__ import annotations

import io
import json
import re
import zipfile

import httpx
import pytest

from fakes import tool
from helpers import arun, record
from test_agent_invariants import _agent
from testgen import (defects, exporters, mcp_hub, notify, pipeline, projects, publisher, sources, storage, suite,
                     testit, trackers)
from testgen.steps import new_step


def _conn(project: dict, preset: str, fields: dict, secrets: dict) -> dict:
    p = projects.get(project["id"])
    c = mcp_hub.new_connection(preset)
    c["fields"] = fields
    p["connections"].append(c)
    projects.save(p)
    mcp_hub.save_secrets(p["id"], c["id"], secrets)
    return c


# ---------- Test IT ----------

class FakeTestIt:
    """Test IT REST API v2, the parts the studio uses."""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path.removeprefix("/api/v2"), request.method
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body))
        assert request.headers["authorization"] == "PrivateToken tok"
        if re.fullmatch(r"/projects/p-1", path):
            return httpx.Response(200, json={"id": "p-1", "name": "Магазин"})
        if path == "/projects/p-1/workItems":
            return httpx.Response(200, json=[{"id": "wi-1", "entityTypeName": "TestCases"},
                                             {"id": "sh-1", "entityTypeName": "SharedSteps"}])
        if path == "/workItems/wi-1":
            return httpx.Response(200, json={
                "id": "wi-1", "globalId": 101, "name": "Вход в систему", "entityTypeName": "TestCases",
                "priority": "High", "preconditionSteps": [{"action": "<p>Пользователь зарегистрирован</p>"}],
                "steps": [{"action": "Открыть страницу входа", "expected": ""},
                          {"action": "Ввести логин и пароль, нажать «Войти»", "expected": "<p>Открыт список: Всего: 2</p>"}]})
        if path == "/autoTests" and method == "POST":
            return httpx.Response(201, json={"id": "auto-1"})
        if path == "/autoTests" and method == "PUT":
            return httpx.Response(204)
        if path == "/autoTests/auto-1/workItems":
            return httpx.Response(204)
        if path == "/projects/p-1/configurations":
            return httpx.Response(200, json=[{"id": "conf-1", "name": "Default"}])
        if path == "/testRuns":
            return httpx.Response(201, json={"id": "run-1"})
        if path.startswith("/testRuns/run-1/"):
            return httpx.Response(204)
        return httpx.Response(404, json={"error": path})


@pytest.fixture
def fake_testit(monkeypatch):
    fake = FakeTestIt()
    monkeypatch.setattr(testit, "TRANSPORT", fake.transport)
    return fake


def test_testit_cases_become_linked_autotests_with_results(stand, project, fake_llm, fake_testit, tmp_path):
    conn = _conn(project, "testit", {"site": "https://testit.test", "project_id": "p-1"}, {"token": "tok"})
    assert arun(mcp_hub.test(project["id"], conn))[0]["name"] == "Test IT: Магазин"
    p = projects.get(project["id"])
    p["pipeline"]["scenarios"]["select"] = "all"
    p["pipeline"]["run"].update(analyze_failures=False, retry_failed=False, trace="off")
    p["pipeline"]["publish"].update(enabled=True, connection=conn["id"], report_runs=True)
    p = projects.update(p["id"], {"pipeline": p["pipeline"]})

    fake_llm.script = _agent([
        ("fill", {"text": "{{username}}", "press_enter": False, "description": "Ввести логин"}, "Логин"),
        ("fill", {"text": "{{password}}", "press_enter": False, "description": "Ввести пароль"}, "Пароль"),
        ("click", {"description": "Нажать «Войти»"}, "Войти"),
        ("assert_element_text", {"text": "Всего: 2", "description": "Открыт список"}, "Всего: 2"),
        ("finish", {"status": "passed", "summary": "ok", "evidence": "проверка результата"}, None)])
    job = pipeline.Job(p, [], "", f"{stand.url}/login.html", {}, cases={"connection": conn["id"], "ids": []})
    arun(job.run())
    assert job.status == "done", (job.error, job.log)
    item = job.items[0]
    assert item["status"] == "done" and item["title"] == "Вход в систему", item
    assert "Открыт список: Всего: 2" in job.scenarios[0]["expected_result"]       # HTML stripped
    t = storage.load(item["test_id"])
    ext = t["external"]["testit"]
    assert ext["work_item_id"] == "wi-1" and ext["autotest_id"] == "auto-1" and ext["linked"]
    sent = {(m, pth) for m, pth, _ in fake_testit.calls}
    assert ("POST", "/autoTests/auto-1/workItems") in sent and ("POST", "/testRuns/run-1/testResults") in sent
    result = next(b for m, pth, b in fake_testit.calls if pth == "/testRuns/run-1/testResults")[0]
    assert result["outcome"] == "Passed" and result["autoTestExternalId"] == t["id"]
    auto = next(b for m, pth, b in fake_testit.calls if pth == "/autoTests" and m == "POST")
    assert "s3cret" not in json.dumps(auto) and auto["externalId"] == t["id"]

    code = exporters.to_playwright(t, testit=True)
    assert "@testit.workItemIds('wi-1')" in code and "with testit.step(" in code and "import testit" in code
    compile(code, "t.py", "exec")
    files = exporters.bundle(p, [t], testit=True)
    assert "testit-adapter-pytest" in files["requirements.txt"] and "privateToken" not in files["connection_config.ini"]


# ---------- Allure TestOps ----------

def test_allure_preset_and_its_destructive_tools():
    preset = mcp_hub.PRESETS["allure"]
    assert preset["url"]({"site": "allure.company.ru"}) == "https://allure.company.ru/api/mcp"
    assert preset["headers"]({"token": "t"}) == {"Authorization": "Api-Token t"}
    for name in ("delete_test_case", "deleteTestCase", "delete_launch", "remove_test_result", "archive_test_case",
                 "close_launch", "bulkDeleteTestCases", "testcase_delete"):
        assert mcp_hub.access(name) == "destructive", name
    assert mcp_hub.access("create_test_case") == "write" and mcp_hub.access("get_test_case") == "read"
    assert mcp_hub.access("search_test_cases") == "read"


# ---------- trackers ----------

class FakeTrackers:
    def __init__(self):
        self.created: list[tuple[str, dict]] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        body = json.loads(request.content) if request.content else None
        if host == "yt.test":
            assert request.headers["authorization"] == "Bearer yt"
            if path == "/api/issues/QA-7":
                return httpx.Response(200, json={"idReadable": "QA-7", "summary": "Корзина", "description": "Добавление товара",
                                                 "customFields": [{"name": "Priority", "value": {"name": "Major"}}],
                                                 "comments": [{"text": "уточнение", "author": {"fullName": "Иван"}}]})
            if path == "/api/admin/projects":
                return httpx.Response(200, json=[{"id": "0-1", "shortName": "QA"}])
            if path == "/api/issues" and request.method == "POST":
                self.created.append(("youtrack", body))
                return httpx.Response(200, json={"idReadable": "QA-99"})
            if path == "/api/users/me":
                return httpx.Response(200, json={"login": "qa", "fullName": "QA Bot"})
        if host == "api.tracker.yandex.net":
            assert request.headers["authorization"] == "OAuth ya" and request.headers["x-org-id"] == "org"
            if path == "/v3/issues/SHOP-3":
                return httpx.Response(200, json={"key": "SHOP-3", "summary": "Оплата", "description": "Оплата картой",
                                                 "status": {"display": "Открыт"}})
            if path == "/v3/issues/SHOP-3/comments":
                return httpx.Response(200, json=[])
        if host == "kaiten.test":
            if path == "/api/latest/cards/55":
                return httpx.Response(200, json={"title": "Личный кабинет", "description": "Профиль пользователя"})
            if path == "/api/latest/cards/55/comments":
                return httpx.Response(200, json=[])
        return httpx.Response(404, json={})


def test_trackers_as_requirements_and_defects(project, monkeypatch):
    fake = FakeTrackers()
    monkeypatch.setattr(trackers, "TRANSPORT", fake.transport)
    yt = _conn(project, "youtrack", {"site": "https://yt.test", "defect_project": "QA"}, {"token": "yt"})
    _conn(project, "yandex_tracker", {"org_id": "org"}, {"token": "ya"})
    _conn(project, "kaiten", {"site": "https://kaiten.test"}, {"token": "k"})
    p = projects.get(project["id"])
    r = arun(sources.fetch("https://yt.test/issue/QA-7/korzina", p))
    assert r["kind"] == "youtrack" and "Добавление товара" in r["text"] and "Priority: Major" in r["text"]
    r = arun(sources.fetch("https://tracker.yandex.ru/SHOP-3", p))
    assert "Оплата картой" in r["text"] and "Статус: Открыт" in r["text"]
    r = arun(sources.fetch("https://kaiten.test/space/1/card/55", p))
    assert "Профиль пользователя" in r["text"]
    assert arun(mcp_hub.test(p["id"], yt))[0]["name"] == "Пользователь: QA Bot"

    test = {"id": "t1", "name": "Корзина", "url": "https://shop.test/cart",
            "steps": [new_step("navigate", "Открыть корзину", "https://shop.test/cart"),
                      new_step("fill", "Ввести пароль", "{{password}}"),
                      new_step("assert_text_present", "Итого 100 ₽", "Итого 100 ₽")]}
    run = {"id": "r1", "started": 0, "browser": "chromium", "events": [{"type": "http", "text": "500 GET /api/cart"}],
           "analysis": {"verdict": "product_bug", "summary": "Сумма корзины не пересчитывается", "suggestion": ""},
           "results": [{"id": test["steps"][0]["id"], "status": "passed"}, {"id": test["steps"][1]["id"], "status": "passed"},
                       {"id": test["steps"][2]["id"], "status": "failed", "error": "Итого 0 ₽", "url": "https://shop.test/cart"}]}
    d = defects.draft(test, run)
    assert d["title"] == "Сумма корзины не пересчитывается"
    assert re.search(r"3\. Итого 100 ₽.*← падение", d["text"])
    assert "{{password}}" not in d["text"] and "500 GET /api/cart" in d["text"]
    res = arun(defects.create(p, yt, d["title"], d["text"]))
    assert res["key"] == "QA-99" and fake.created[0][1]["project"] == {"id": "0-1"}


# ---------- specification files ----------

def _docx(paragraphs: list[tuple[str, str]], table: list[list[str]]) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>' if style
                   else f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for style, text in paragraphs)
    body += "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row)
                                + "</w:tr>" for row in table) + "</w:tbl>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {w}><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def _pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)


def test_specification_files():
    doc = _docx([("Heading1", "Техническое задание"), ("", "Пользователь входит по логину и паролю."),
                 ("ListParagraph", "Пароль не короче 8 символов")], [["Поле", "Правило"], ["Email", "обязательное"]])
    r = sources.from_file("ТЗ на вход.docx", doc)
    assert r["title"] == "ТЗ на вход" and "## Техническое задание" in r["text"]
    assert "- Пароль не короче 8 символов" in r["text"] and "| Email | обязательное |" in r["text"]
    r = sources.from_file("spec.pdf", _pdf("Login form requirements"))
    assert "Login form requirements" in r["text"]
    with pytest.raises(sources.SourceError):
        sources.from_file("spec.exe", b"MZ")


# ---------- notifications ----------

class FakeSmtp:
    sent: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        assert password == "smtp-pass"

    def send_message(self, msg):
        FakeSmtp.sent.append(msg)


def test_notifications_on_suite_failures_streak_and_proposals(stand, project, save_test, monkeypatch):
    sent: list[tuple[str, dict]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.host, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(notify, "TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setattr(notify, "SMTP", FakeSmtp)
    FakeSmtp.sent = []
    notify.save(project["id"], {"telegram": {"enabled": True, "chat_id": "-100"},
                                "mattermost": {"enabled": True},
                                "email": {"enabled": True, "host": "smtp.test", "port": 587, "user": "qa",
                                          "sender": "qa@test", "to": "lead@test"},
                                "events": {"suite": "failed", "streak": 2, "proposals": True, "budget": True}},
                {"telegram_token": "123:abc", "mattermost_webhook": "https://mm.test/hooks/x",
                 "smtp_password": "smtp-pass"})
    assert notify.public_view(projects.get(project["id"]))["secrets_set"] == ["mattermost_webhook", "smtp_password",
                                                                             "telegram_token"]
    p = projects.get(project["id"])
    p["pipeline"]["run"].update(analyze_failures=False, retry_failed=False, self_heal=False, trace="off")
    p = projects.update(p["id"], {"pipeline": p["pipeline"]})
    bad = save_test("Сломанный", [new_step("navigate", "open", f"{stand.url}/list.html"),
                                  new_step("assert_text_present", "Нет такого", "Нет такого текста")])
    for _ in range(2):
        s = suite.new(p, [bad])
        arun(suite.run(p, s, [bad]))
    texts = [b.get("text", "") for _, b in sent]
    assert sum("набор — есть падения" in t for t in texts) == 4           # 2 suites x telegram + mattermost
    assert any("падает 2 раз подряд" in t for t in texts)
    assert {h for h, _ in sent} == {"api.telegram.org", "mm.test"}
    assert any("Сломанный" in m.get_content() for m in FakeSmtp.sent)

    # A passing suite with suite="failed" sends nothing; proposals do.
    sent.clear()
    good = save_test("Хороший", [new_step("navigate", "open", f"{stand.url}/list.html")])
    s = suite.new(p, [good])
    arun(suite.run(p, s, [good]))
    assert not sent
    assert "ждёт ревью" in notify.message(p, "proposals", test=good, run={"proposals": 2})[0]
