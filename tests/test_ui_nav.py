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
import types
import uuid

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from testgen import agent, auth, pipeline, projects, storage, tasks
from testgen.steps import new_step

# the menu in the order of work: requirements, pipeline, Studio, tests | tasks, dashboard | project
TABS = ["req", "pipe", "studio", "tests", "tasks", "dash", "proj"]


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
    page.wait_for_load_state("networkidle")     # loggedIn() ends by entering the project: a click before it is lost


def test_tabs_switch_without_errors(page, studio_url, project):
    _open(page, studio_url, project)
    for tab in TABS:
        page.click(f"nav button[data-tab={tab}]")
        expect(page.locator(f"#tab-{tab}")).to_be_visible()
        expect(page.locator(f"nav button[data-tab={tab}]")).to_have_class("on")
    page.click("#allProj")
    expect(page.locator("#tab-home")).to_be_visible()
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


# ---- addresses of screens (stage 1): F5, «Назад»/«Вперёд», links, the missing

def test_reload_keeps_the_screen(page, studio_url, project, save_test):
    t = save_test("Тест с адресом", [new_step("navigate", "open", project["base_url"])])
    _open(page, studio_url, project)
    page.click("nav button[data-tab=tests]")
    expect(page).to_have_url(f"{studio_url}/#/p/{project['id']}/tests")
    page.locator("#testsBody a", has_text="Тест с адресом").click()
    expect(page).to_have_url(f"{studio_url}/#/p/{project['id']}/tests/{t['id']}")
    page.reload()
    expect(page.locator("#tab-tests")).to_be_visible()
    expect(page.locator("#testBox")).to_contain_text("Тест с адресом")

    page.click("nav button[data-tab=proj]")
    page.locator(".subnav button[data-sub=process]").click()
    expect(page).to_have_url(f"{studio_url}/#/p/{project['id']}/settings/process")
    page.reload()
    expect(page.locator("[data-subpanel=process]")).to_be_visible()
    assert page.errors == []


def test_back_and_forward_walk_the_entities(page, studio_url, project, save_test):
    a = save_test("Первый", [new_step("navigate", "open", project["base_url"])])
    b = save_test("Второй", [new_step("navigate", "open", project["base_url"])])
    _open(page, studio_url, project)
    page.click("nav button[data-tab=tests]")
    page.locator("#testsBody a", has_text="Первый").click()
    expect(page.locator("#testBox h3")).to_have_text("Первый")
    page.locator("#testsBody a", has_text="Второй").click()
    expect(page.locator("#testBox h3")).to_have_text("Второй")
    page.click("nav button[data-tab=dash]")
    expect(page.locator("#tab-dash")).to_be_visible()

    page.go_back()
    expect(page.locator("#testBox h3")).to_have_text("Второй")
    page.go_back()
    expect(page.locator("#testBox h3")).to_have_text("Первый")
    expect(page).to_have_url(f"{studio_url}/#/p/{project['id']}/tests/{a['id']}")
    page.go_forward()
    expect(page).to_have_url(f"{studio_url}/#/p/{project['id']}/tests/{b['id']}")
    expect(page.locator("#testBox h3")).to_have_text("Второй")
    page.locator("#testBox button[aria-label=Закрыть]").click()
    expect(page.locator("#testBox")).to_be_hidden()
    expect(page).to_have_url(f"{studio_url}/#/p/{project['id']}/tests")
    assert page.errors == []


def test_missing_or_foreign_address_says_not_found(page, studio_url, project, save_test):
    _open(page, studio_url, project)
    page.goto(f"{studio_url}/#/p/{project['id']}/tests/nosuchtest")
    expect(page.locator("#tab-missing")).to_be_visible()
    expect(page.locator("#missingLink")).to_have_text("К списку")
    page.click("#missingLink")
    expect(page.locator("#tab-tests")).to_be_visible()

    page.goto(f"{studio_url}/#/p/nosuchproject/tests")
    page.reload()
    expect(page.locator("#tab-missing")).to_be_visible()
    expect(page.locator("#missingLink")).to_have_text("К проектам")
    page.click("#missingLink")
    expect(page.locator("#tab-home")).to_be_visible()


def test_old_notification_links_open_the_screen(page, studio_url, project, save_test):
    t = save_test("Из уведомления", [new_step("navigate", "open", project["base_url"])])
    page.goto(f"{studio_url}/#test={t['id']}")
    expect(page.locator("#testBox h3")).to_have_text("Из уведомления")
    expect(page).to_have_url(f"{studio_url}/#/p/{project['id']}/tests/{t['id']}")
    assert page.errors == []


# ---- the bell «Ждёт вас» (stage 2)

def test_bell_counts_decisions_and_opens_them(page, studio_url, project, save_test, monkeypatch):
    step = new_step("click", "Войти", "", [{"kind": "text", "value": "Войти"}])
    t = save_test("Лечится", [step], heal_proposals=[{
        "id": "p1", "step_id": step["id"], "test_id": "", "module": False, "description": "Войти", "action": "click",
        "old": step["locator"], "new": [{"kind": "role", "role": "button", "name": "Вход"}], "reason": "кнопку переименовали",
        "screenshot": "", "at": time.time()}])
    job = {"id": "c3" * 5, "project_id": project["id"], "status": "awaiting_model", "created": time.time(),
           "waiting_since": time.time(), "items": []}
    monkeypatch.setitem(pipeline.JOBS, job["id"], types.SimpleNamespace(id=job["id"], project_id=project["id"],
                                                                        state=lambda: job))
    _open(page, studio_url, project)
    page.click("nav button[data-tab=dash]")
    expect(page.locator("#bellCount")).to_have_text("2")
    expect(page.locator("nav button[data-tab=pipe] .badge")).to_have_text("1")

    page.click("#bellBtn")
    menu = page.locator("#bellMenu")
    expect(menu).to_contain_text("Запуски ждут решения")
    expect(menu).to_contain_text("подтверждения жизненного цикла")
    menu.get_by_role("menuitem", name="«Лечится»: самолечение ждёт ревью").click()
    expect(menu).to_be_hidden()
    expect(page).to_have_url(f"{studio_url}/#/p/{project['id']}/tests/{t['id']}")
    expect(page.locator("#testBox h3")).to_have_text("Лечится")

    # Decided elsewhere: the next look (the menu opened, the tab back, the timer) has it gone.
    job["status"] = "running"
    storage.update(t["id"], lambda x: x.update(heal_proposals=[]))
    page.click("#bellBtn")
    expect(menu).to_contain_text("Вашего решения ничего не ждёт")
    expect(page.locator("#bellCount")).to_have_text("0")
    expect(page.locator("nav button[data-tab=pipe] .badge")).to_be_hidden()
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    assert page.errors == []


# ---- several Studio sessions, where a session came from (stage 3)

def test_sessions_of_two_projects_switch_with_the_project(page, studio_url, project, monkeypatch):
    import server
    other = projects.get(projects.create("Второй " + uuid.uuid4().hex[:6], base_url=project["base_url"])["id"])
    task = tasks.create(project["id"], {"title": "Покрыть корзину"})
    made = []
    for p, name in ((project, "Сессия один"), (other, "Сессия два")):
        s = agent.StudioSession(p, name, p["base_url"], "Сценарий")
        s.user, s.status = auth.ANONYMOUS, "idle"
        monkeypatch.setitem(server.SESSIONS, s.id, s)
        made.append(s)
    made[0].task_id = task["id"]
    _open(page, studio_url, project)
    page.click("nav button[data-tab=studio]")
    expect(page.locator("#wsTitle")).to_have_text("Сессия один")
    expect(page.locator("#wsSource")).to_contain_text("Из задачи «Покрыть корзину»")
    expect(page.locator("#sessBar .sess-chip")).to_have_count(2)

    # A session of another project: the project is switched with it.
    page.locator("#sessBar .sess-chip", has_text="Сессия два").click()
    expect(page.locator("#projSel")).to_have_value(other["id"])
    expect(page.locator("#wsTitle")).to_have_text("Сессия два")
    expect(page).to_have_url(f"{studio_url}/#/p/{other['id']}/studio/{made[1].id}")
    # Choosing the project shows its own session.
    page.select_option("#projSel", project["id"])
    expect(page.locator("#wsTitle")).to_have_text("Сессия один")
    # «+ Ещё сессия»: the form of a new test, the sessions stay open.
    page.click("#sessBar >> text=+ Ещё сессия")
    expect(page.locator("#startForm")).to_be_visible()
    expect(page.locator("#sessBar .sess-chip")).to_have_count(2)
    assert page.errors == []


# ---- the menu by the stages of work and the first start (stage 5)

def test_menu_follows_the_work_and_a_project_opens_where_it_was_left(page, studio_url, project):
    _open(page, studio_url, project)
    assert page.locator("nav button:visible").evaluate_all("bs => bs.map(b => b.dataset.tab)") == TABS
    expect(page.locator("#tab-req")).to_be_visible()             # the first time - the requirements
    page.click("nav button[data-tab=tests]")
    page.click("#allProj")
    expect(page).to_have_url(f"{studio_url}/#/projects")
    page.locator("#projCards .card", has_text=project["name"]).get_by_role("button", name="Открыть").click()
    expect(page.locator("#tab-tests")).to_be_visible()          # the tab it was left on
    page.reload()
    expect(page.locator("#tab-tests")).to_be_visible()
    page.goto(studio_url)                                        # no address: the last project, its last tab
    expect(page.locator("#tab-tests")).to_be_visible()
    assert page.errors == []


def test_wizard_ends_with_the_first_step_and_studio_is_five_clicks_away(page, studio_url, project):
    _open(page, studio_url, project)
    name = "Магазин " + uuid.uuid4().hex[:6]
    page.click("#allProj")
    page.click("#homeNew")                                       # 1
    page.fill("#wzName", name)
    page.fill("#wzUrl", project["base_url"])
    page.click("#wz1Next")                                       # 2
    page.locator("[data-wz='2'] [data-wzgo='3']").click()        # 3
    page.locator("[data-wz='3'] [data-wzgo='4']").click()        # 4
    page.locator("#wzStart [data-start=studio]").click()         # 5 - then «Начать» starts the agent
    expect(page.locator("#tab-studio")).to_be_visible()
    expect(page.locator("#startForm")).to_be_visible()
    expect(page.locator("#fUrl")).to_have_value(project["base_url"])
    expect(page.locator("#fScenario")).to_be_focused()
    made = next(p for p in projects.list_projects() if p["name"] == name)
    assert projects.get(made["id"])["start"] == "studio"
    assert page.errors == []


def test_pipeline_takes_requirements_from_their_tab_only(page, studio_url, project):
    _open(page, studio_url, project)
    page.click("nav button[data-tab=pipe]")
    form = page.locator("#pipeForm")
    expect(form.locator("#pipeReviewNote")).to_contain_text("Сначала проверьте требования")
    expect(form.locator("textarea")).to_have_count(0)            # no new requirements here
    expect(page.locator("[data-pipe-design]")).to_be_visible()
    page.get_by_role("radio", name="Сценарии из анализа").click()
    expect(page.locator("[data-pipe-src=picks]")).to_be_visible()
    expect(page.locator("[data-pipe-design]")).to_be_hidden()   # the scenarios are designed already
    page.get_by_role("radio", name="Обход сайта").click()
    expect(page.locator("#pipeStart")).to_have_text("Исследовать сайт")
    page.get_by_role("radio", name="Проверенные требования").click()
    page.locator("#pipeReviewNote a", has_text="Проверить требования").click()
    expect(page.locator("#tab-req")).to_be_visible()
    expect(page.locator("#reqNew")).to_have_attribute("open", "")
    assert page.errors == []
