"""Smoke test of the studio's interface (static/index.html): the page in a real browser.

The studio runs in this process (uvicorn in a thread, login off), Playwright opens it: the tabs switch
without errors in the console, the shared messages (toast, ask) work. The stages of docs/usability-plan.md
add their checks here (addresses of screens, the bell, several sessions). Marked `browser` by the `project`
fixture (it needs the stand).
"""
from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from testgen import auth, storage
from testgen.steps import new_step

TABS = ["studio", "tests", "dash", "tasks", "req", "pipe", "proj", "home"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def studio_url():
    import server
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not srv.started:
        if time.time() > deadline or not thread.is_alive():
            raise RuntimeError("студия не запустилась")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(10)


@pytest.fixture
def page(studio_url, monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", False)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        pg = browser.new_page(viewport={"width": 1440, "height": 900})
        pg.errors = []
        pg.on("console", lambda m: m.type == "error" and pg.errors.append(m.text))
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        yield pg
        browser.close()


def _open(page, studio_url, project) -> None:
    page.add_init_script(f"localStorage.setItem('tg_project', {project['id']!r})")
    page.goto(studio_url)
    expect(page.locator("nav")).to_be_visible()
    expect(page.locator("#projSel")).to_have_value(project["id"])
    page.wait_for_load_state("networkidle")     # loggedIn() ends with showTab('home'): a click before it is lost


def test_tabs_switch_without_errors(page, studio_url, project):
    _open(page, studio_url, project)
    for tab in TABS:
        page.click(f"nav button[data-tab={tab}]")
        expect(page.locator(f"#tab-{tab}")).to_be_visible()
        expect(page.locator(f"nav button[data-tab={tab}]")).to_have_class("on")
    page.wait_for_load_state("networkidle")
    assert page.errors == []


def test_delete_test_asks_and_reports(page, studio_url, project, save_test):
    t = save_test("Удаляемый тест", [new_step("navigate", "open", project["base_url"])])
    _open(page, studio_url, project)
    page.click("nav button[data-tab=tests]")
    row = page.locator("#testsBody tr", has_text="Удаляемый тест")

    def delete():
        if not row.locator("details.menu[open]").count():
            row.locator("details.menu summary").click()
        row.get_by_role("button", name="Удалить").click()

    dlg = page.locator("#askDlg")
    delete()
    expect(dlg).to_be_visible()
    expect(dlg.locator("#askSubject")).to_have_text("Удаляемый тест")
    expect(dlg.get_by_role("button", name="Отмена")).to_be_focused()   # Enter does not delete by accident
    page.keyboard.press("Escape")
    expect(dlg).to_be_hidden()
    expect(row).to_have_count(1)

    delete()
    dlg.get_by_role("button", name="Удалить тест").click()
    expect(page.locator("#toasts .toast")).to_contain_text("Тест «Удаляемый тест» удалён")
    expect(row).to_have_count(0)
    assert page.errors == []
    assert storage.load(t["id"]) is None
