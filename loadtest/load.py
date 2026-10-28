"""Load test of the studio's API: virtual users do what people do in the studio, step by step more of them.

    python -m loadtest.load --url http://127.0.0.1:8766 --stages 25,50,100,200 --hold 60 --out report.json

Each user opens the studio (who am I, projects, «Ждёт вас»), picks a project and then, with a pause of `--think`
seconds between actions, opens the test list (with filters and search), a test with its run history, tasks, the
dashboard, the application model, requirements and runs; now and then changes tags, comments a test or moves a task.
«Ждёт вас» is polled every 30 s, as the page does. No model and no browser: Studio sessions and runs are not part
of the load. A stage passes when p95 < --p95 ms and errors < --errors %; the exit code is 1 if one does not."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import statistics
import sys
import time
from collections import defaultdict

import httpx


class Stats:
    def __init__(self):
        self.lat: dict[str, list[float]] = defaultdict(list)
        self.err: dict[str, int] = defaultdict(int)
        self.codes: dict[str, int] = defaultdict(int)
        self.start = time.monotonic()

    def add(self, name: str, ms: float, ok: bool, code: str) -> None:
        self.lat[name].append(ms)
        if not ok:
            self.err[name] += 1
            self.codes[code] += 1


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def summary(s: Stats, seconds: float) -> dict:
    allx = [x for v in s.lat.values() for x in v]
    n, e = len(allx), sum(s.err.values())
    ep = {k: {"n": len(v), "rps": round(len(v) / seconds, 2), "p50": round(pct(v, 50)), "p95": round(pct(v, 95)),
              "p99": round(pct(v, 99)), "max": round(max(v)), "errors": s.err.get(k, 0)}
          for k, v in sorted(s.lat.items(), key=lambda kv: -pct(kv[1], 95))}
    return {"requests": n, "rps": round(n / seconds, 1), "p50": round(pct(allx, 50)), "p95": round(pct(allx, 95)),
            "p99": round(pct(allx, 99)), "mean": round(statistics.fmean(allx)) if allx else 0,
            "errors": e, "error_pct": round(100 * e / n, 2) if n else 0.0, "codes": dict(s.codes), "endpoints": ep}


class User:
    def __init__(self, n: int, client: httpx.AsyncClient, world: dict, args, stop: asyncio.Event):
        self.n, self.c, self.w, self.a, self.stop = n, client, world, args, stop
        self.rnd = random.Random(n)
        self.stats: Stats | None = None

    async def req(self, name: str, method: str, url: str, **kw):
        t = time.monotonic()
        try:
            r = await self.c.request(method, url, **kw)
            ok, code = r.status_code < 400, str(r.status_code)
        except httpx.HTTPError as e:
            r, ok, code = None, False, type(e).__name__
        if self.stats is not None and not self.stop.is_set():
            self.stats.add(name, (time.monotonic() - t) * 1000, ok, code)
        return r

    async def think(self) -> None:
        lo, hi = self.a.think
        try:
            await asyncio.wait_for(self.stop.wait(), self.rnd.uniform(lo, hi))
        except asyncio.TimeoutError:
            pass

    async def poll_inbox(self, pid: str) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), 30)
            except asyncio.TimeoutError:
                await self.req("GET /api/projects/{pid}/inbox", "GET", f"/api/projects/{pid}/inbox")
                await self.req("GET /api/inbox", "GET", "/api/inbox")

    async def run(self) -> None:
        await self.req("GET /api/auth/me", "GET", "/api/auth/me")
        await self.req("GET /api/projects", "GET", "/api/projects")
        await self.req("GET /api/inbox", "GET", "/api/inbox")
        pid = self.rnd.choice(list(self.w))
        tids, task_ids = self.w[pid]["tests"], self.w[pid]["tasks"]
        await self.req("GET /api/projects/{pid}", "GET", f"/api/projects/{pid}")
        await self.req("GET /api/projects/{pid}/inbox", "GET", f"/api/projects/{pid}/inbox")
        poller = asyncio.create_task(self.poll_inbox(pid))
        acts = [(self.tests_list, 30), (self.open_test, 25), (self.tasks, 10), (self.dashboard, 8),
                (self.knowledge, 5), (self.requirements, 5), (self.runs, 5), (self.write, 12)]
        fns, weights = zip(*acts)
        while not self.stop.is_set():
            await self.rnd.choices(fns, weights)[0](pid, tids, task_ids)
            await self.think()
        poller.cancel()

    async def tests_list(self, pid, tids, task_ids):
        params = {"project_id": pid}
        x = self.rnd.random()
        if x < 0.15:
            params["q"] = str(self.rnd.randint(1, 99))
        elif x < 0.30:
            params["health"] = self.rnd.choice(["failing", "flaky", "quarantined", "outdated"])
        elif x < 0.40:
            params["status"] = self.rnd.choice(["draft", "review", "ready"])
        name = "GET /api/tests" + ("?" + ",".join(k for k in params if k != "project_id") if len(params) > 1 else "")
        await self.req(name, "GET", "/api/tests", params=params)

    async def open_test(self, pid, tids, task_ids):
        tid = self.rnd.choice(tids)
        await self.req("GET /api/tests/{tid}", "GET", f"/api/tests/{tid}")
        await self.req("GET /api/tests/{tid}/runs", "GET", f"/api/tests/{tid}/runs")
        await self.req("GET /api/tests/{tid}/links", "GET", f"/api/tests/{tid}/links")
        if self.rnd.random() < 0.2:
            await self.req("GET /api/tests/{tid}/versions", "GET", f"/api/tests/{tid}/versions")

    async def tasks(self, pid, tids, task_ids):
        await self.req("GET /api/projects/{pid}/tasks", "GET", f"/api/projects/{pid}/tasks")
        if task_ids and self.rnd.random() < 0.5:
            tid = self.rnd.choice(task_ids)
            await self.req("GET /api/tasks/{tid}", "GET", f"/api/tasks/{tid}")

    async def dashboard(self, pid, tids, task_ids):
        await self.req("GET /api/projects/{pid}/metrics", "GET", f"/api/projects/{pid}/metrics")

    async def knowledge(self, pid, tids, task_ids):
        await self.req("GET /api/projects/{pid}/knowledge", "GET", f"/api/projects/{pid}/knowledge")

    async def requirements(self, pid, tids, task_ids):
        await self.req("GET /api/projects/{pid}/analyses", "GET", f"/api/projects/{pid}/analyses")
        await self.req("GET /api/projects/{pid}/jobs", "GET", f"/api/projects/{pid}/jobs")

    async def runs(self, pid, tids, task_ids):
        await self.req("GET /api/projects/{pid}/suites", "GET", f"/api/projects/{pid}/suites")
        await self.req("GET /api/projects/{pid}/coverage", "GET", f"/api/projects/{pid}/coverage")

    async def write(self, pid, tids, task_ids):
        x, tid = self.rnd.random(), self.rnd.choice(tids)
        if x < 0.4:
            tags = self.rnd.sample(["smoke", "regress", "cart", "auth", "load"], 2)
            await self.req("PATCH /api/tests/{tid}/meta", "PATCH", f"/api/tests/{tid}/meta", json={"tags": tags})
        elif x < 0.7:
            await self.req("POST /api/tests/{tid}/comments", "POST", f"/api/tests/{tid}/comments",
                           json={"text": f"Комментарий нагрузки {self.n}"})
        elif task_ids:
            t = self.rnd.choice(task_ids)
            await self.req("PATCH /api/tasks/{tid}", "PATCH", f"/api/tasks/{t}",
                           json={"status": self.rnd.choice(["todo", "in_progress", "review"])})


async def discover(c: httpx.AsyncClient, limit: int) -> dict:
    ps = (await c.get("/api/projects")).raise_for_status().json()
    ps = ps.get("projects", ps) if isinstance(ps, dict) else ps
    world = {}
    for p in ps[:limit]:
        tests = (await c.get("/api/tests", params={"project_id": p["id"]}, timeout=120)).raise_for_status().json()
        tasks = (await c.get(f"/api/projects/{p['id']}/tasks")).json()
        tasks = tasks.get("tasks", tasks) if isinstance(tasks, dict) else tasks
        if tests:
            world[p["id"]] = {"tests": [t["id"] for t in tests], "tasks": [t["id"] for t in tasks or []]}
    if not world:
        sys.exit("В студии нет проектов с тестами: сначала python -m loadtest.seed")
    return world


def show(stage: int, s: dict, a) -> bool:
    ok = s["p95"] <= a.p95 and s["error_pct"] <= a.errors
    print(f"\n=== {stage} пользователей: {s['requests']} запросов, {s['rps']} RPS, p50 {s['p50']} мс, "
          f"p95 {s['p95']} мс, p99 {s['p99']} мс, ошибок {s['errors']} ({s['error_pct']}%) — "
          f"{'OK' if ok else 'НЕ ПРОШЁЛ'}")
    if s["codes"]:
        print("   коды ошибок:", s["codes"])
    print(f"   {'запрос':<44}{'n':>7}{'p50':>8}{'p95':>8}{'p99':>8}{'max':>8}{'ош.':>6}")
    for k, v in list(s["endpoints"].items())[:a.top]:
        print(f"   {k:<44}{v['n']:>7}{v['p50']:>8}{v['p95']:>8}{v['p99']:>8}{v['max']:>8}{v['errors']:>6}")
    return ok


async def main_async(a) -> int:
    limits = httpx.Limits(max_connections=max(a.stages) + 20, max_keepalive_connections=max(a.stages) + 20)
    headers = {"Authorization": f"Bearer {a.token}"} if a.token else {}
    # Several instances of the studio on one database (a team install): users are spread over them, as by a balancer
    clients = [httpx.AsyncClient(base_url=u, timeout=a.timeout, limits=limits, headers=headers) for u in a.url]
    async with contextlib.AsyncExitStack() as stack:
        for c in clients:
            await stack.enter_async_context(c)
        world = await discover(clients[0], a.projects)
        print(f"Проектов: {len(world)}, тестов: {sum(len(w['tests']) for w in world.values())}")
        stop, users, report, passed = asyncio.Event(), [], [], True
        tasks = []
        for stage in a.stages:
            stats = Stats()
            for u in users:
                u.stats = stats
            new = [User(n, clients[n % len(clients)], world, a, stop) for n in range(len(users), stage)]
            for i, u in enumerate(new):          # ramp the new users in evenly over --ramp seconds
                u.stats = stats
                users.append(u)
                tasks.append(asyncio.create_task(u.run()))
                await asyncio.sleep(a.ramp / max(1, len(new)))
            stats = Stats()                      # measure the stage once everyone is in
            for u in users:
                u.stats = stats
            await asyncio.sleep(a.hold)
            s = summary(stats, time.monotonic() - stats.start)
            s["users"] = stage
            passed = show(stage, s, a) and passed
            report.append(s)
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"url": a.url, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "think": a.think,
                       "thresholds": {"p95_ms": a.p95, "error_pct": a.errors}, "stages": report}, f,
                      ensure_ascii=False, indent=1)
        print(f"\nОтчёт: {a.out}")
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8766", help="адрес студии; несколько экземпляров — через запятую")
    ap.add_argument("--token", default="", help="API-токен (python -m testgen.auth token …), если вход включён")
    ap.add_argument("--stages", default="25,50,100,200", help="число пользователей по ступеням")
    ap.add_argument("--ramp", type=float, default=20, help="секунд на подключение новых пользователей ступени")
    ap.add_argument("--hold", type=float, default=60, help="секунд замера на ступени")
    ap.add_argument("--think", default="1,3", help="пауза пользователя между действиями, с: от,до")
    ap.add_argument("--projects", type=int, default=10, help="сколько проектов студии нагружать")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--p95", type=float, default=1000, help="порог p95, мс")
    ap.add_argument("--errors", type=float, default=1.0, help="порог доли ошибок, %%")
    ap.add_argument("--top", type=int, default=15, help="сколько самых медленных запросов показать")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    a.url = a.url.split(",")
    a.stages = sorted(int(x) for x in a.stages.split(","))
    a.think = tuple(float(x) for x in a.think.split(","))
    sys.exit(asyncio.run(main_async(a)))


if __name__ == "__main__":
    main()
