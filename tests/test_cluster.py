"""The stage 5 criterion, as real processes: two instances of the studio and three workers on one
database. A suite started through one instance is spread over the workers and read through the
other; a Studio session is reached through either instance.

Needs a shared database: TESTGEN_TEST_DB (a PostgreSQL URL); skipped otherwise."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from testgen import db, fs, storage, workqueue
from testgen.steps import new_step

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not fs.remote(), reason="needs TESTGEN_TEST_DB (a shared database)")


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(check, timeout=180, what=""):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = check()
            if v:
                return v
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"timeout: {what}")


@pytest.fixture
def cluster(tmp_path):
    env = os.environ | {"TESTGEN_AUTH": "off", "TESTGEN_EMBEDDED_WORKER": "off", "PYTHONUNBUFFERED": "1",
                        "TESTGEN_WORKER_CONCURRENCY": "1"}
    procs, urls = [], []
    try:
        for i in range(2):
            port = _port()
            url = f"http://127.0.0.1:{port}"
            urls.append(url)
            procs.append(subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env | {
                "PORT": str(port), "TESTGEN_INSTANCE_URL": url}, stdout=open(tmp_path / f"web{i}.log", "w"),
                stderr=subprocess.STDOUT))
        for i in range(3):
            procs.append(subprocess.Popen([sys.executable, "-m", "testgen.worker"], cwd=ROOT, env=env,
                                          stdout=open(tmp_path / f"worker{i}.log", "w"), stderr=subprocess.STDOUT))
        for url in urls:
            _wait(lambda url=url: httpx.get(f"{url}/api/ready", timeout=5).json()["ok"], 90, f"{url} ready")
        _wait(lambda: workqueue.stats()["workers"] >= 3, 90, "three workers")
        yield urls
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                p.kill()


def test_two_instances_three_workers(cluster, stand, tmp_path):
    a, b = cluster
    p = httpx.post(f"{a}/api/projects", json={"name": f"Кластер {stand.url[-5:]}", "base_url": stand.url}).json()
    for i in range(6):
        storage.save({"project_id": p["id"], "name": f"Тест {i}", "url": stand.url, "scenario": "",
                      "steps": [new_step("navigate", "open", f"{stand.url}/form.html"),
                                new_step("assert_url_contains", "form", "form.html")]})
    sid = httpx.post(f"{b}/api/projects/{p['id']}/runs", json={}).json()["id"]
    s = _wait(lambda: (x := httpx.get(f"{a}/api/suites/{sid}").json())["status"] != "running" and x, 240, "suite")
    assert s["passed"] and s["summary"]["passed"] == 6, s
    run_ids = [i["run_id"] for i in s["items"]]
    with db.engine().connect() as c:
        who = {w for (w,) in c.execute(db.work.select().with_only_columns(db.work.c.worker)
                                       .where(db.work.c.kind == "run", db.work.c.ref.in_(run_ids)))}
    assert len(who) >= 2, who                       # the tests went to different workers
    # A step screenshot of a run made in a worker process, read through the other web instance.
    run = httpx.get(f"{b}/api/runs/{s['items'][0]['run_id']}").json()
    shot = httpx.get(f"{b}/api/runs/{run['id']}/files/{run['results'][0]['shot']}")
    assert shot.status_code == 200 and shot.content[:2] == b"\xff\xd8"

    # A Studio session lives in instance A; B passes requests about it there.
    httpx.put(f"{b}/api/projects/{p['id']}/llm", json={"model": "test-model"}).raise_for_status()
    sess = httpx.post(f"{a}/api/sessions", json={"project_id": p["id"], "url": f"{stand.url}/form.html",
                                                 "scenario": "Открыть форму", "name": "Кластер"}).json()["id"]
    state = _wait(lambda: (x := httpx.get(f"{b}/api/sessions/{sess}").json()).get("id") == sess and x, 60, "session")
    assert state["id"] == sess
    assert httpx.delete(f"{b}/api/sessions/{sess}").json()["ok"]
    assert httpx.get(f"{a}/api/sessions/{sess}").status_code == 404
