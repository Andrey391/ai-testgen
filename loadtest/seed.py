"""Synthetic data for the studio's load test: its own PostgreSQL database with projects, tests, run history and tasks.

    python -m loadtest.seed --projects 5 --tests 1000 --runs 20 --tasks 200
    -> prints the database URL to start the studio with (TESTGEN_DATABASE_URL)

The database (`testgen_load` by default) is created anew on the server of TESTGEN_LOAD_SERVER (default: the local
testgen:testgen@127.0.0.1:5432), so the load never touches the studio's real data. No model, no browser."""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

SERVER = os.environ.get("TESTGEN_LOAD_SERVER", "postgresql+psycopg://testgen:testgen@127.0.0.1:5432/postgres")


def recreate(name: str) -> str:
    e = create_engine(SERVER, isolation_level="AUTOCOMMIT")
    with e.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{name}"'))
    e.dispose()
    return make_url(SERVER).set(database=name).render_as_string(hide_password=False)


def _steps(n: int) -> list[dict]:
    from testgen.steps import new_step
    actions = [("navigate", "Открыть страницу", "/catalog"), ("click", "Нажать «Купить»", ""),
               ("fill", "Ввести email", "{{faker.email}}"), ("assert_visible", "Видна корзина", ""),
               ("assert_text_present", "Есть текст «Заказ оформлен»", "Заказ оформлен")]
    out = []
    for i in range(n):
        a, d, v = actions[i % len(actions)]
        s = new_step(a, f"{d} #{i}", v)
        s["locator"] = [f"[data-testid='el-{i}']", f"#el{i}", f"role=button[name='Кнопка {i}']"]
        out.append(s)
    return out


def seed(n_projects: int, n_tests: int, n_runs: int, n_tasks: int) -> list[str]:
    from testgen import db, projects, storage, tasks
    from testgen.repo import runs as runs_repo
    db.upgrade()
    rnd, pids, now = random.Random(7), [], time.time()
    tags = ["smoke", "regress", "cart", "auth", "profile", "search", "api", "admin"]
    for p in range(n_projects):
        pr = projects.create(f"Нагрузка {p + 1}", "Синтетический проект нагрузочного теста",
                             "http://127.0.0.1:8780")
        pids.append(pr["id"])
        tids = []
        t0 = time.time()
        for i in range(n_tests):
            t = storage.save({"project_id": pr["id"], "name": f"Тест {i}: оформление заказа {rnd.randint(1, 999)}",
                              "url": "http://127.0.0.1:8780/", "scenario": "Пользователь оформляет заказ",
                              "steps": _steps(rnd.randint(5, 25)), "tags": rnd.sample(tags, 2),
                              "status": rnd.choice(["draft", "review", "ready", "ready"]),
                              "layer": "api" if i % 10 == 0 else "ui"})
            tids.append(t["id"])
            rows, passed = [], True
            for r in range(n_runs):
                passed = rnd.random() > (0.3 if i % 7 == 0 else 0.05)
                st = now - (n_runs - r) * 3600
                rows.append({"id": uuid.uuid4().hex[:12], "project_id": pr["id"], "test_id": t["id"],
                             "status": "passed" if passed else "failed", "trigger": "suite", "started": st,
                             "finished": st + rnd.uniform(5, 60), "passed": passed, "results": []})
            with db.engine().begin() as c:
                for row in rows:
                    runs_repo.Sql.put(c, row)
            if rows:
                last = rows[-1]
                storage.update(t["id"], lambda x, last=last: x.__setitem__("last_run", {
                    "at": last["finished"], "run_id": last["id"], "status": last["status"],
                    "passed": last["passed"], "flaky": False, "healed": 0, "failed_step": "", "error": ""}))
            if (i + 1) % 200 == 0:
                print(f"  {pr['name']}: {i + 1}/{n_tests} тестов, {time.time() - t0:.0f} с", flush=True)
        for k in range(n_tasks):
            tasks.create(pr["id"], {"title": f"Задача {k}", "priority": rnd.choice(["low", "medium", "high"]),
                                    "status": rnd.choice(["todo", "in_progress", "review", "done"]),
                                    "test_ids": rnd.sample(tids, min(3, len(tids)))})
        print(f"{pr['name']} ({pr['id']}): {n_tests} тестов, {n_tests * n_runs} прогонов, {n_tasks} задач", flush=True)
    return pids


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="testgen_load", help="имя базы (создаётся заново)")
    ap.add_argument("--projects", type=int, default=5)
    ap.add_argument("--tests", type=int, default=1000, help="тестов на проект")
    ap.add_argument("--runs", type=int, default=20, help="прогонов на тест")
    ap.add_argument("--tasks", type=int, default=200, help="задач на проект")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    url = recreate(a.db)
    os.environ["TESTGEN_DATABASE_URL"] = url
    t0 = time.time()
    seed(a.projects, a.tests, a.runs, a.tasks)
    print(f"Готово за {time.time() - t0:.0f} с. Студия для нагрузки:\n  TESTGEN_DATABASE_URL={url}", file=sys.stderr)


if __name__ == "__main__":
    main()
