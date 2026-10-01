"""Shared storage and scale (stage 5.5): the database and S3 behind fs.py, locks between processes,
the work queue and workers, requests passed to the instance that holds a Studio session, metrics.

The database here is SQLite of the temporary folder (as with TESTGEN_TEST_DB=sqlite), S3 is moto."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

import boto3
import httpx
import pytest
from fastapi.testclient import TestClient

from testgen import audit, db, fs, projects, runs, storage, suite, vault, workqueue
from testgen import worker as worker_mod
from testgen.paths import DATA
from testgen.steps import new_step

KEY = "shared storage tests: a long secret key"


@pytest.fixture(scope="module")
def s3_server():
    from moto.server import ThreadedMotoServer
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    endpoint = "http://%s:%d" % server.get_host_and_port()
    boto3.client("s3", endpoint_url=endpoint, aws_access_key_id="t", aws_secret_access_key="t",
                 region_name="us-east-1").create_bucket(Bucket="scale-tests")
    yield endpoint
    server.stop()


@pytest.fixture
def shared(monkeypatch, tmp_path, s3_server):
    """The shared storage for this test: its own SQLite database and bucket prefix."""
    monkeypatch.setenv("TESTGEN_DATABASE_URL", f"sqlite:///{(tmp_path / 'shared.db').as_posix()}")
    monkeypatch.setenv("TESTGEN_SECRET_KEY", KEY)
    for k, v in {"TESTGEN_S3_ENDPOINT": s3_server, "TESTGEN_S3_BUCKET": "scale-tests", "TESTGEN_S3_ACCESS_KEY": "t",
                 "TESTGEN_S3_SECRET_KEY": "t", "TESTGEN_S3_PREFIX": uuid.uuid4().hex[:8]}.items():
        monkeypatch.setenv(k, v)
    return boto3.client("s3", endpoint_url=s3_server, aws_access_key_id="t", aws_secret_access_key="t",
                        region_name="us-east-1")


def _objects(s3) -> list[str]:
    import os
    prefix = os.environ["TESTGEN_S3_PREFIX"] + "/"
    return [o["Key"][len(prefix):] for o in s3.list_objects_v2(Bucket="scale-tests", Prefix=prefix).get("Contents", [])]


def test_files_live_in_the_database_and_s3(shared):
    d = DATA / "projects" / "pscale" / "tests"
    fs.write_json(d / "t1.json", {"id": "t1", "name": "Первый"})
    fs.write_json(d / "t2.json", {"id": "t2", "name": "Второй"})
    shot = DATA / "projects" / "pscale" / "runs" / "t1" / "r1" / "step-1.jpg"
    fs.write_bytes(shot, b"\xff\xd8jpeg")
    assert not (d / "t1.json").exists()                       # nothing on the local disk
    assert fs.read_json(d / "t1.json")["name"] == "Первый" and fs.read_json(d / "nope.json", 7) == 7
    assert sorted(p.name for p in fs.glob(d, "*.json")) == ["t1.json", "t2.json"]
    assert [p.name for p in fs.glob(DATA / "projects", "*/tests/t2.json")] == ["t2.json"]
    assert [json.loads(text)["id"] for _, text, _ in fs.documents(d)] == ["t2", "t1"]      # newest first
    assert "data/projects/pscale/runs/t1/r1/step-1.jpg" in _objects(shared)
    with db.engine().connect() as c:
        kinds = dict(c.execute(db.docs.select().with_only_columns(db.docs.c.name, db.docs.c.kind)).all())
    assert kinds == {"t1.json": "json", "t2.json": "json", "step-1.jpg": "s3"}
    assert fs.exists(DATA / "projects" / "pscale") and fs.is_dir(d) and not fs.is_file(d)
    assert [p.name for p in fs.iterdir(DATA / "projects" / "pscale")] == ["runs", "tests"]

    # The browser's files: brought to the local disk when needed, sent back after a run.
    local = fs.local_path(shot)
    assert local.read_bytes() == b"\xff\xd8jpeg"
    trace = shot.parent / "trace.zip"
    trace.write_bytes(b"PK-trace")
    assert fs.push(shot.parent) == 1 and fs.push(shot.parent) == 0
    trace.unlink()
    assert fs.local_path(trace).read_bytes() == b"PK-trace"

    fs.rmtree(DATA / "projects" / "pscale")
    assert not fs.exists(DATA / "projects" / "pscale") and _objects(shared) == []


def test_locks_between_threads_and_processes(shared):
    f = DATA / "counter.json"
    fs.write_json(f, {"n": 0})

    def bump():
        for _ in range(15):
            with fs.lock(f):
                with fs.lock(f):                     # re-entrant
                    n = fs.read_json(f)["n"]
                fs.write_json(f, {"n": n + 1})
    threads = [threading.Thread(target=bump) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fs.read_json(f)["n"] == 90


def test_import_and_export_an_installation(shared, tmp_path):
    old = tmp_path / "old-data"
    (old / "projects" / "p1" / "tests").mkdir(parents=True)
    (old / "projects" / "p1" / "project.json").write_text(json.dumps({"id": "p1", "name": "Старый"}), "utf-8")
    (old / "projects" / "p1" / "tests" / "a.json").write_text('{"id": "a"}', "utf-8")
    (old / "projects" / "p1" / "baselines" / "a").mkdir(parents=True)
    (old / "projects" / "p1" / "baselines" / "a" / "s.png").write_bytes(b"\x89PNG")
    assert fs.import_tree(old, DATA) == 3
    assert fs.read_json(DATA / "projects" / "p1" / "project.json")["name"] == "Старый"
    assert fs.read_bytes(DATA / "projects" / "p1" / "baselines" / "a" / "s.png") == b"\x89PNG"
    out = tmp_path / "backup"
    fs.export_tree(DATA / "projects" / "p1", out)
    assert (out / "baselines" / "a" / "s.png").read_bytes() == b"\x89PNG"
    fs.rmtree(DATA / "projects" / "p1")

    # The command: secrets of the old folder, written in plain text, are encrypted on the way in.
    old_secrets = tmp_path / "old-secrets"
    (old_secrets / "projects" / "p2").mkdir(parents=True)
    (old_secrets / "projects" / "p2" / "app.json").write_text('{"password": "plain-old-pass"}', "utf-8")
    db._cli(["import-files", str(tmp_path / "empty"), str(old_secrets)])
    with db.engine().connect() as c:
        body = c.execute(db.docs.select().where(db.docs.c.path == "secrets/projects/p2/app.json")).first().body
    assert "plain-old-pass" not in body and vault.load("projects/p2", "app") == {"password": "plain-old-pass"}


def test_secrets_in_the_database_are_encrypted(shared, monkeypatch):
    vault.save("projects/pv", "app", {"password": "db-secret-1"})
    with db.engine().connect() as c:
        body = c.execute(db.docs.select().where(db.docs.c.path == "secrets/projects/pv/app.json")).first().body
    assert "db-secret-1" not in body and json.loads(body)["enc"] == "aes-256-gcm"
    assert vault.load("projects/pv", "app") == {"password": "db-secret-1"}
    monkeypatch.delenv("TESTGEN_SECRET_KEY")
    with pytest.raises(vault.VaultError, match="зашифрованными"):
        vault.save("projects/pv", "app", {"password": "x"})


def test_audit_chain_in_the_database(shared):
    for i in range(4):
        audit.record("test.update", user="u", project_id="p", target={"tid": str(i)})
    assert audit.verify() == {"ok": True, "records": 4, "error": ""}
    assert audit.read(limit=1)[0]["target"] == {"tid": "3"}
    with db.engine().begin() as c:
        row = c.execute(db.audit_log.select().order_by(db.audit_log.c.id).limit(1).offset(1)).first()
        rec = json.loads(row.rec) | {"user": "mallory"}
        c.execute(db.audit_log.update().where(db.audit_log.c.id == row.id).values(rec=json.dumps(rec)))
    assert "изменена" in audit.verify()["error"]


def test_queue_items_go_to_one_worker_each(shared):
    ids = {workqueue.put("verify", {"project_id": "p", "test_id": str(i)}, "p", str(i)) for i in range(24)}
    got: list[str] = []

    def take(name):
        while True:
            items = workqueue.claim(name, 2)
            if not items:
                return
            got.extend(i["id"] for i in items)
    threads = [threading.Thread(target=take, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == sorted(ids)                                     # every item once, none twice
    assert workqueue.stats()["running"] == 24 and workqueue.active("5")


def test_dead_worker_items_fail_their_runs(shared, project, save_test):
    t = save_test("Мёртвый воркер", [new_step("navigate", "open", project["base_url"])])
    run = runs.new(t, "manual", live=False)
    worker_mod.start_run(project, t, run, submit=None)
    (item,) = workqueue.claim("ghost", 1)
    with db.engine().begin() as c:
        c.execute(db.work.update().values(heartbeat=time.time() - workqueue.DEAD - 5))
    assert runs.get(run["id"])["status"] == "running"                     # queued work is not "abandoned"
    for dead in workqueue.reap():
        worker_mod.fail_item(dead, "Воркер перестал отвечать")
    r = runs.get(run["id"])
    assert r["status"] == "error" and "Воркер" in r["error"] and item["id"]


def _start_workers(n: int) -> list[tuple[worker_mod.Worker, threading.Thread]]:
    out = []
    for i in range(n):
        w = worker_mod.Worker(1, name=f"test-worker-{i}")
        t = threading.Thread(target=lambda w=w: asyncio.new_event_loop().run_until_complete(w.serve()), daemon=True)
        t.start()
        out.append((w, t))
    return out


def _wait(check, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        v = check()
        if v:
            return v
        time.sleep(0.5)
    raise AssertionError("timeout")


def test_workers_run_tests_and_spread_a_suite(shared, stand, project, save_test):
    tests = [save_test(f"Набор {i}", [new_step("navigate", "open", f"{stand.url}/form.html"),
                                      new_step("assert_url_contains", "form", "form.html")]) for i in range(4)]
    workers = _start_workers(2)
    try:
        run = runs.new(tests[0], "manual", live=False)
        worker_mod.start_run(project, tests[0], run, submit=None)
        r = _wait(lambda: (x := runs.get(run["id"])) and x["status"] != "running" and x)
        assert r["status"] == "passed", r
        shot = r["results"][0]["shot"]
        local = runs.files_dir(r) / shot
        local.unlink()                                          # as seen from another machine
        assert runs.file(r, shot).read_bytes()[:2] == b"\xff\xd8"
        assert storage.load(tests[0]["id"])["last_run"]["status"] == "passed"

        s = suite.new(project, tests, user="alice")
        worker_mod.start_suite(project, s, tests, submit=None)
        s = _wait(lambda: (x := suite.get(s["id"])) and x["status"] != "running" and x)
        assert s["passed"] and s["summary"]["passed"] == 4, s
        with db.engine().connect() as c:
            who = {w for (w,) in c.execute(db.work.select().with_only_columns(db.work.c.worker)
                                           .where(db.work.c.kind == "run"))}
        assert len(who) == 2                                    # the suite's tests went to both workers
        assert workqueue.stats()["workers"] == 2
    finally:
        for w, _ in workers:
            w.stopping = True
        for _, t in workers:
            t.join(60)


def test_requests_about_a_session_go_to_its_instance(shared, monkeypatch):
    import server
    seen = []

    def other_instance(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "s-remote", "status": "idle", "served_by": "b"})
    monkeypatch.setattr(server, "INSTANCE_URL", "http://studio-a:8765")
    monkeypatch.setattr(server, "PROXY_TRANSPORT", httpx.MockTransport(other_instance))
    monkeypatch.setattr(server.auth, "ENABLED", False)
    workqueue.set_owner("session", "s-remote", "http://studio-b:8765")
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    r = client.get("/api/sessions/s-remote?x=1")
    assert r.json()["served_by"] == "b"
    assert str(seen[0].url) == "http://studio-b:8765/api/sessions/s-remote?x=1"
    assert seen[0].headers[server.PROXIED] == "http://studio-a:8765"
    # Not found anywhere: this instance answers.
    assert client.get("/api/sessions/nobody").status_code == 404
    workqueue.drop_owner("session", "s-remote")
    assert client.get("/api/sessions/s-remote").status_code == 404


def test_metrics_and_readiness(shared, monkeypatch):
    import server
    monkeypatch.setattr(server.auth, "ENABLED", False)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    client.get("/api/projects")
    workqueue.put("verify", {"project_id": "p", "test_id": "t"}, "p", "t")
    text = client.get("/metrics").text
    assert 'testgen_http_requests_total{method="GET",route="/api/projects",status="200"}' in text
    assert 'testgen_queue_items{status="queued"} 1.0' in text
    ready = client.get("/api/ready").json()
    assert ready["ok"] and ready["storage"] == "database" and ready["database"] and ready["s3"]
    monkeypatch.setenv("TESTGEN_METRICS_TOKEN", "m3trics")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer m3trics"}).status_code == 200
    projects.list_projects()
