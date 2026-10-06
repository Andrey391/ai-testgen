"""Tables of their own for runs, tests, tasks and model spending (repo/, migration 0003): the move of
the documents into them and back, filters in SQL, read-modify-write without lost updates, rotation of
the run history, import and export of an installation.

Each test gets an empty database of its own (conftest.new_database)."""
from __future__ import annotations

import json
import threading
import time
import types
import uuid

import conftest
import pytest

from testgen import db, fs, llm, projects, repo, runs, storage, tasks


@pytest.fixture
def new_database(monkeypatch):
    """Switch to an empty database (conftest.new_database), dropped after the test."""
    made: list[str] = []
    monkeypatch.setenv("TESTGEN_SECRET_KEY", "repository tests: a long enough key")
    monkeypatch.delenv("TESTGEN_S3_BUCKET", raising=False)

    def switch() -> str:
        made.append(conftest.new_database("repo"))
        monkeypatch.setenv("TESTGEN_DATABASE_URL", made[-1])
        return made[-1]
    yield switch
    for url in made:
        conftest.drop_database(url)


@pytest.fixture
def database(new_database, tmp_path):
    new_database()
    return tmp_path


def _pid() -> str:
    return uuid.uuid4().hex[:8]


def _paths(prefix: str) -> set[str]:
    with db.engine().connect() as c:
        return {p for (p,) in c.execute(db.docs.select().with_only_columns(db.docs.c.path)
                                        .where(db.docs.c.path.like(prefix + "%")))}


def _counts(**tokens) -> types.SimpleNamespace:
    return types.SimpleNamespace(**(dict.fromkeys(llm.FIELDS, 0) | tokens))


def test_migration_moves_documents_into_tables_and_back(database, monkeypatch):
    monkeypatch.setenv("TESTGEN_DB_MIGRATE", "off")
    db.upgrade(db.engine(), "0002")
    pid = _pid()
    p = projects.path(pid)
    fs.write_json(p / "project.json", {"id": pid, "name": "Старая схема"})
    fs.write_json(p / "tests" / "t1.json", {"id": "t1", "project_id": pid, "name": "Вход", "tags": ["smoke"],
                                            "steps": [], "updated": 5})
    fs.write_json(p / "tasks" / "a1b2c3d4e5.json", {"id": "a1b2c3d4e5", "project_id": pid, "title": "Покрыть вход",
                                                    "status": "todo", "priority": "high", "test_ids": ["t1"],
                                                    "created": 1, "updated": 1})
    run = {"id": "0123456789", "project_id": pid, "test_id": "t1", "status": "passed", "trigger": "manual",
           "suite_id": "", "started": 10.0, "finished": 12.5, "passed": True, "flaky": False, "quarantined": False,
           "healed": 0, "proposals": 0, "attempts": [{"passed": True}], "results": []}
    fs.write_json(p / "runs" / "t1" / "0123456789.json", run)
    fs.write_json(p / "runs" / "t1" / "index.json", [runs.summary(run)])
    fs.write_bytes(p / "runs" / "t1" / "0123456789" / "step-1.jpg", b"\xff\xd8")
    ledger = {"requests": 3, "stages": {"authoring": {"m": {"input_tokens": 100, "cache_creation_input_tokens": 0,
                                                            "cache_read_input_tokens": 40, "output_tokens": 7,
                                                            "requests": 3}}}}
    fs.write_json(p / "usage" / "2026-09.json", ledger)

    db.upgrade(db.engine())
    assert storage.load("t1")["name"] == "Вход" and storage.select(pid, tags=["smoke"])[0]["id"] == "t1"
    assert tasks.load("a1b2c3d4e5")["title"] == "Покрыть вход"
    assert runs.history(pid, "t1") == [runs.summary(run)] and runs.get("0123456789")["finished"] == 12.5
    assert llm.ledger(pid, "2026-09") == ledger
    # Moved out of `docs`; the project and the files of the run stay there.
    assert _paths(f"data/projects/{pid}/") == {f"data/projects/{pid}/project.json",
                                               f"data/projects/{pid}/runs/t1/0123456789/step-1.jpg"}

    db.upgrade(db.engine(), "0002", down=True)
    assert fs.read_json(p / "tests" / "t1.json")["name"] == "Вход"
    assert fs.read_json(p / "runs" / "t1" / "index.json") == [runs.summary(run)]
    assert fs.read_json(p / "usage" / "2026-09.json") == ledger
    assert fs.read_json(p / "tasks" / "a1b2c3d4e5.json")["test_ids"] == ["t1"]
    with db.engine().connect() as c:
        assert not db.engine().dialect.has_table(c, "runs")


def test_tests_tasks_and_spending_in_tables(database):
    p = projects.create("Таблицы")
    pid = p["id"]
    smoke = storage.save({"project_id": pid, "name": "Smoke", "steps": [], "tags": ["smoke"]})
    draft = storage.save({"project_id": pid, "name": "Черновик", "steps": [], "tags": ["smoke"], "status": "draft"})
    module = storage.save({"project_id": pid, "name": "Модуль", "steps": [], "role": "module"})
    assert [t["id"] for t in storage.select(pid, tags=["smoke"])] == [smoke["id"]]
    assert {t["id"] for t in storage.select(pid, tags=["smoke"], include_drafts=True)} == {smoke["id"], draft["id"]}
    assert [t["id"] for t in storage.select(pid, test_ids=[draft["id"], module["id"]])] == [draft["id"]]
    assert storage.names(pid)[module["id"]] == "Модуль" and storage.counts()[pid] == 3
    assert projects.list_projects()[0]["tests"] == 3
    with db.engine().connect() as c:
        row = c.execute(db.tests.select().where(db.tests.c.id == draft["id"])).first()
    assert row.status == "draft" and row.tags == ["smoke"] and row.body["name"] == "Черновик"

    # A change keeps the previous version; the version history stays in documents.
    storage.update(smoke["id"], lambda t: t.update(name="Smoke 2"))
    assert storage.load(smoke["id"])["name"] == "Smoke 2"
    assert [v["name"] for v in storage.versions(smoke)] == ["Smoke"]

    task = tasks.create(pid, {"title": "Покрыть оплату", "assignee": "alice"})
    tasks.create(pid, {"title": "Готово", "status": "done"})
    assert tasks.link_test(task["id"], smoke["id"])["status"] == "in_progress"
    assert tasks.counts(pid) == {"todo": 0, "in_progress": 1, "review": 0, "done": 1, "open": 1}
    (only,) = tasks.list_tasks(pid, "open", "alice")
    assert only["tests"] == [{"id": smoke["id"], "name": "Smoke 2"}]
    storage.delete(smoke["id"])
    assert tasks.load(task["id"])["test_ids"] == [] and storage.load(smoke["id"]) is None

    llm.ledger_add(pid, "authoring", "m", _counts(input_tokens=10, output_tokens=2))
    llm.ledger_add(pid, "authoring", "m", _counts(input_tokens=5, cache_read_input_tokens=3))
    llm.ledger_add(pid, "heal", "m", _counts(output_tokens=1))
    led = llm.ledger(pid)
    assert led["requests"] == 3 and led["stages"]["authoring"]["m"] == {
        "input_tokens": 15, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3, "output_tokens": 2,
        "requests": 2}

    projects.delete(pid)
    with db.engine().connect() as c:
        for t in (db.tests, db.tasks, db.usage, db.runs):
            assert c.execute(t.select().where(t.c.project_id == pid)).first() is None


def test_updates_from_threads_are_not_lost(database):
    pid = projects.create("Потоки")["id"]
    t = storage.save({"project_id": pid, "name": "Счётчик", "steps": [], "n": 0})
    task = tasks.create(pid, {"title": "Счётчик"})
    others = [storage.save({"project_id": pid, "name": f"Т{i}", "steps": []})["id"] for i in range(6)]

    def bump(i):
        for _ in range(10):
            storage.update(t["id"], lambda x: x.update(n=x["n"] + 1))
        tasks.link_test(task["id"], others[i])
    threads = [threading.Thread(target=bump, args=(i,)) for i in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert storage.load(t["id"])["n"] == 60
    assert sorted(tasks.load(task["id"])["test_ids"]) == sorted(others)


def test_run_history_is_rows(database):
    pid = projects.create("Прогоны")["id"]
    a = storage.save({"project_id": pid, "name": "А", "steps": []})
    b = storage.save({"project_id": pid, "name": "Б", "steps": []})
    made = []
    for i in range(5):
        r = runs.new(a, "manual", live=False)
        r.update(status="passed" if i % 2 else "failed", passed=bool(i % 2), started=100 + i)
        fs.write_bytes(runs.files_dir(r) / "step-1.jpg", b"\xff\xd8")
        runs.finish(r, keep=3)
        made.append(r["id"])
        time.sleep(0.01)
    rb = runs.new(b, "suite", "s1")
    assert runs.get(rb["id"])["status"] == "running" and runs.history(pid, b["id"]) == []     # not finished
    runs.finish(rb | {"status": "passed", "passed": True})

    hist = runs.history(pid, a["id"], limit=0)
    assert [h["id"] for h in hist] == made[2:] and [h["outcomes"] for h in hist] == [[False], [True], [False]]
    assert runs.get(made[0]) is None and not fs.exists(runs.files_dir({"project_id": pid, "test_id": a["id"],
                                                                       "id": made[0]}))
    assert runs.histories(pid, [a["id"], b["id"]], limit=2) == {a["id"]: hist[-2:],
                                                                 b["id"]: runs.history(pid, b["id"])}
    assert runs.flip_rate(hist) == 1.0
    listed = {t["id"]: t for t in storage.list_tests(pid)}
    assert len(listed[a["id"]]["recent"]) == 3 and listed[b["id"]]["recent"][0]["trigger"] == "suite"
    with db.engine().connect() as c:
        row = c.execute(db.runs.select().where(db.runs.c.id == rb["id"])).first()
    assert row.suite_id == "s1" and row.duration >= 0 and row.report["test_name"] == "Б"
    assert not _paths(f"data/projects/{pid}/runs/{a['id']}/index")          # no index document any more

    storage.delete(a["id"])
    assert runs.history(pid, a["id"]) == [] and runs.get(made[-1]) is None


def test_export_and_import_keep_the_tables(database, new_database, tmp_path):
    pid = projects.create("Перенос")["id"]
    t = storage.save({"project_id": pid, "name": "Перенос", "steps": [], "tags": ["ui"]})
    r = runs.new(t, "manual", live=False)
    runs.finish(r | {"status": "passed", "passed": True})
    task = tasks.create(pid, {"title": "Задача"})
    llm.ledger_add(pid, "authoring", "m", _counts(input_tokens=7))
    month = llm._month()

    out = tmp_path / "backup"
    db._cli(["export-files", str(out)])
    folder = out / "data" / "projects" / pid
    assert json.loads((folder / "tests" / f"{t['id']}.json").read_text("utf-8"))["tags"] == ["ui"]
    assert json.loads((folder / "runs" / t["id"] / "index.json").read_text("utf-8"))[0]["id"] == r["id"]
    assert (folder / "tasks" / f"{task['id']}.json").is_file() and (folder / "usage" / f"{month}.json").is_file()

    new_database()
    assert storage.load(t["id"]) is None
    db._cli(["import-files", str(out / "data"), str(out / "secrets")])
    assert storage.load(t["id"])["tags"] == ["ui"] and tasks.load(task["id"])["title"] == "Задача"
    assert [h["id"] for h in runs.history(pid, t["id"])] == [r["id"]]
    assert llm.ledger(pid, month)["stages"]["authoring"]["m"]["input_tokens"] == 7
    assert repo.owns(f"data/projects/{pid}/runs/{t['id']}/{r['id']}.json")
    assert not repo.owns(f"data/projects/{pid}/runs/{t['id']}/{r['id']}/trace.json")
