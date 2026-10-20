"""«Импорт кейсов» (cases.py): manual cases of a test management system are imported for a person to choose,
reviewed (the role and test data of each, what keeps the agent from automating it), and a run of the pipeline waits
for a person to settle the blocking findings; the answers reach the agent with the case."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from fakes import Resp
from helpers import arun
from test_integrations import FakeTestIt, _conn
from testgen import auth, cases, knowledge, pipeline, projects, storage, testit


@pytest.fixture(scope="module")
def client():
    import server
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


@pytest.fixture
def open_studio(monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", False)


@pytest.fixture
def fake_testit(monkeypatch):
    fake = FakeTestIt()
    monkeypatch.setattr(testit, "TRANSPORT", fake.transport)
    return fake


@pytest.fixture
def shop(fake_testit):
    p = projects.create("Кейсы " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium", "prices": {"test-model": [5, 25]}})
    conn = _conn(p, "testit", {"site": "https://testit.test", "project_id": "p-1"}, {"token": "tok"})
    return projects.get(p["id"]), conn


def test_cases_are_imported_for_a_person_to_choose(client, open_studio, shop):
    p, conn = shop
    r = client.post(f"/api/projects/{p['id']}/cases/fetch", json={"connection": conn["id"], "ids": ""})
    assert r.status_code == 200, r.text
    imp = r.json()
    assert [c["title"] for c in imp["cases"]] == ["Вход в систему"] and imp["cases"][0]["test"] is None
    assert "Открыт список: Всего: 2" in imp["cases"][0]["expected_result"]
    # A case automated before shows its test: a person sees it before automating the case again.
    t = storage.save({"project_id": p["id"], "name": "Вход", "url": p["base_url"], "scenario": "", "steps": [],
                      "external": {"testit": {"case_id": "wi-1", "work_item_id": "wi-1"}}})
    again = client.get(f"/api/projects/{p['id']}/cases/imports/{imp['id']}").json()
    assert again["cases"][0]["test"] == {"id": t["id"], "name": "Вход"}
    # Nothing chosen - nothing to automate; an import of another project is not found.
    assert client.post(f"/api/projects/{p['id']}/jobs", json={"case_import": imp["id"], "case_picks": []}).status_code == 400
    other = projects.create("Чужой " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")
    assert client.get(f"/api/projects/{other['id']}/cases/imports/{imp['id']}").status_code == 404


def _review(kw) -> Resp:
    return Resp(parsed=cases.XCases(cases=[cases.XCase(
        index=0, role="Покупатель", test_data=[cases.XData(entity="Товар", name="Молоко", state="в наличии")],
        findings=[cases.XFinding(kind="data", severity="major", step=2, quote="Ввести логин и пароль",
                                 problem="Не сказано, под каким пользователем входить",
                                 suggestion="Войти под учётной записью роли «Покупатель»"),
                  cases.XFinding(kind="ambiguous", severity="blocker", problem="Не ясно, что значит «список»",
                                 question="Какой список должен открыться?"),
                  cases.XFinding(kind="other", severity="minor", problem="Нет номера версии")])]))


def test_a_run_waits_for_the_findings_and_gives_the_answers_to_the_agent(shop, fake_llm):
    p, conn = shop
    imp = cases.save_import(p["id"], conn, arun(testit.import_cases(conn["fields"], {"token": "tok"})))
    fake_llm.script = lambda kind, kw: _review(kw)
    job = pipeline.Job(p, [], "", "", {}, cases={"connection": conn["id"], "import_id": imp["id"], "picks": [0]})
    arun(job._import_cases(p))
    assert len(job.items) == 1 and job.items[0]["scenario_index"] == 0
    arun(job._review_cases(p))
    sc = job.scenarios[0]
    assert sc["role"] == "Покупатель" and sc["test_data"][0]["name"] == "Молоко"
    assert len(case_ids := [f["id"] for f in sc["case_review"]["findings"]]) == 3
    assert len(cases.unsettled(job.scenarios)) == 2          # the minor finding does not block
    asked = len(fake_llm.calls)
    arun(job._review_cases(p))                               # reviewed cases are not asked again
    assert len(fake_llm.calls) == asked

    async def go():
        task = asyncio.create_task(job._await_cases())
        for _ in range(100):
            if job.status == "awaiting_cases":
                break
            await asyncio.sleep(0.02)
        assert job.status == "awaiting_cases"
        # an answer needs its text; a proposal is taken as the reviewer wrote it
        assert job.settle_cases([{"id": case_ids[1], "action": "answer", "text": ""}], "ann") == 0
        job.settle_cases([{"id": case_ids[0], "action": "accept"}], "ann")
        await asyncio.sleep(0.05)
        assert not task.done()                               # one blocking finding is left
        job.settle_cases([{"id": case_ids[1], "action": "answer", "text": "Список товаров каталога"}], "ann")
        await asyncio.wait_for(task, 5)
    arun(go())
    assert job.status == "running" and job.waits[-1]["status"] == "awaiting_cases"
    text = pipeline.scenario_text(sc)
    assert "Войти под учётной записью роли «Покупатель»" in text and "Список товаров каталога" in text
    assert "Роль: Покупатель" in text
    # the test data of the case joined the application model: the agent gets the id of the record
    rec = sc["test_data"][0]["record_id"]
    assert rec and any(d["id"] == rec for d in knowledge.get(p["id"])["data"]) and f"[id {rec}]" in text
    assert cases.tally(job.scenarios) | {"cases": 1} == {"cases": 1, "findings": 3, "blocking": 2, "unsettled": 0,
                                                          "answered": 2, "dismissed": 0}
