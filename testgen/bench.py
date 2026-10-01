"""The benchmark: how good are the tests a model writes (stages 1.5 and 4.1).

    python -m testgen.bench --provider gigachat --model GigaChat-2-Max
    python -m testgen.bench --site stand-login --site stand-list          # a part of the set
    python -m testgen.bench --local-only                                 # only the local stand
    python -m testgen.bench compare bench/results/*.json bench/manual.json --html comparison.html

For every scenario of bench/sites.json:
  1. generation: the agent writes the test in Auto-Pilot (time, cost, steps);
  2. three runs on the clean site, without self-healing: the test works right away;
  3. a run with the scenario's injected defect: the test must FAIL on it;
  4. on the local stand, a run on the "v2" layout with self-healing (review mode);
  5. mutation testing of its assertions (mutations.py).

Metrics (docs/bench.md): working test from the first generation (3 of 3 clean runs), catches the
injected defect, share of killed mutants, survives the layout change, human edits (in Auto-Pilot 0;
from Studio sessions it is test["authoring_stats"]["edits"]), time to a ready test, cost of a test in
$ and ₽. The result goes to bench/results/<date>-<model>.json and .html, and its success rate is
stored with the model (providers.record_bench): the Auto-Pilot gate for models other than Claude.

It costs money (every scenario is a real generation): run it by hand, not on every push.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import html
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

from . import llm, mailbox, mutations, projects, providers, runner, storage
from .agent import StudioSession, has_assertion
from .paths import ROOT

SITES = ROOT / "bench" / "sites.json"
RESULTS = ROOT / "bench" / "results"
AUTHORING_TIMEOUT = 600
TARGETS = {"first_try": 0.70, "catches_defect": 0.80, "mutation_score": 0.70, "survives_v2": 0.85,
           "edits": 2, "minutes": 10}

_SWALLOW = """(() => { const want = %s.toLowerCase();
  const hit = el => { for (; el && el !== document; el = el.parentNode || el.host) {
      if (el.nodeType === 1 && el.matches('a, button, input, label, summary, [role=button], [onclick], [draggable=true]')
          && (el.innerText || el.value || el.getAttribute('aria-label') || '').toLowerCase().includes(want)) return true; }
    return false; };
  for (const t of ['click', 'dblclick'])
    window.addEventListener(t, e => { if (hit(e.composedPath()[0])) { e.preventDefault(); e.stopImmediatePropagation(); } }, true);
})();"""

_HIDE = """(() => { const want = %s;
  const strip = root => { const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) if (n.nodeValue.includes(want)) n.nodeValue = n.nodeValue.split(want).join(''); };
  const start = () => { strip(document.body); new MutationObserver(ms => ms.forEach(m => {
      m.addedNodes.forEach(n => n.nodeType === 3 ? (n.nodeValue.includes(want) && (n.nodeValue = n.nodeValue.split(want).join(''))) : strip(n));
      if (m.type === 'characterData' && m.target.nodeValue.includes(want)) m.target.nodeValue = m.target.nodeValue.split(want).join(''); }))
    .observe(document.body, {childList: true, subtree: true, characterData: true}); };
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', start) : start();
})();"""


class DefectHooks:
    """runner.run_test hooks that break the site the way the scenario's "defect" says."""

    def __init__(self, defect: dict):
        self.defect = defect

    async def setup(self, bs) -> None:
        d = self.defect
        kind = d.get("kind")
        if kind == "http_500":
            async def fail(route):
                await route.fulfill(status=500, content_type="application/json", body='{"error": "bench defect"}')
            await bs.context.route(d["url"], fail)
        elif kind == "swallow_click":
            await bs.context.add_init_script(_SWALLOW % json.dumps(d.get("text", "")))
        elif kind == "hide_text":
            await bs.context.add_init_script(_HIDE % json.dumps(d.get("text", "")))
        elif kind == "script":
            await bs.context.add_init_script(d["js"])

    async def before_step(self, bs, i, step, loc) -> None:
        pass


def load_sites(path: Path = SITES) -> list[dict]:
    return json.loads(path.read_text("utf-8"))["sites"]


def _stand():
    """The local stand of the studio's own tests (tests/stand.py), started in this process."""
    spec = importlib.util.spec_from_file_location("testgen_bench_stand", ROOT / "tests" / "stand.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Stand()


def bench_project(provider: str, model: str) -> dict:
    name = "Бенчмарк"
    p = next((projects.get(x["id"]) for x in projects.list_projects() if x["name"] == name), None) or \
        projects.create(name, "Тесты бенчмарка моделей (python -m testgen.bench)")
    p["pipeline"]["authoring"].update(provider=provider, model=model, autopilot=True, autopilot_min_success=0,
                                      max_steps=40)
    p["pipeline"]["run"].update(provider=provider, model=model, self_heal=True, heal_mode="review",
                                analyze_failures=False, retry_failed=False, trace="off")
    p["pipeline"]["verify"].update(mutants=5)
    return projects.update(p["id"], {"pipeline": p["pipeline"]})


def _prepare(project: dict, site: dict, stand_url: str) -> dict:
    creds = dict(site.get("credentials") or {})
    projects.set_app_credentials(project["id"], creds.get("username", ""), creds.get("password", ""),
                                 creds.get("totp_secret", ""))
    mb = site.get("mailbox")
    mailbox.set_mailbox(project["id"], {**mb, "url": mb["url"].replace("{stand}", stand_url)} if mb else {"kind": ""})
    files = projects.path(project["id"]) / "files"
    for name, text in (site.get("files") or {}).items():
        files.mkdir(parents=True, exist_ok=True)
        (files / name).write_bytes(text.encode("utf-8"))
    return {k: v for k, v in creds.items() if v}


async def _author(project: dict, site: dict, sc: dict, url: str, creds: dict, headless: bool) -> tuple[StudioSession, float]:
    task = f"{sc['instructions']}\nОжидаемый результат: {sc['expected']}"
    s = StudioSession(project, f"{site['id']}/{sc['id']}", url, task, headless=headless, credentials=creds)
    started = time.monotonic()
    try:
        await s.start()
        s.set_autopilot(True)
        while time.monotonic() - started < AUTHORING_TIMEOUT:
            if s.status in ("done", "error") or (not s.autopilot and s.status in ("idle", "awaiting_approval")):
                break
            await asyncio.sleep(0.5)
    except Exception as e:
        s.status = "error"
        s.chat.append({"role": "system", "text": llm.api_error_text(e)})
    return s, time.monotonic() - started


async def run_scenario(project: dict, site: dict, sc: dict, stand=None, headless: bool = True, log=print) -> dict:
    stand_url = stand.url if stand else ""
    if "{stand}" in site["url"] and not stand:
        return {"site": site["id"], "id": sc["id"], "skipped": "нет локального стенда"}
    url = site["url"].replace("{stand}", stand_url)
    if stand:
        stand.reset()
    creds = _prepare(project, site, stand_url)
    out = {"site": site["id"], "id": sc["id"], "title": site["title"], "instructions": sc["instructions"]}
    with llm.usage_scope() as usage:
        s, seconds = await _author(project, site, sc, url, creds, headless)
        out.update(authored=s.status == "done", finish=s.finish_status, seconds=round(seconds, 1),
                   steps=len([x for x in s.steps if x.get("status") != "failed"]), edits=s.edits,
                   error=next((m["text"] for m in reversed(s.chat) if m["role"] == "system"), "")
                   if s.status != "done" else "")
        test = None
        if s.status == "done" and s.finish_status == "passed" and has_assertion(s.steps):
            test, _ = s.save()
        await s.close()
        if test is None:
            out.update(first_try=False, catches_defect=None, survives_v2=None, mutation_score=None)
            out["cost"] = usage.as_dict()
            log(f"  ✘ {site['id']}/{sc['id']}: тест не записан ({s.status}/{s.finish_status}) {out['error'][:120]}")
            return out
        clean_cfg = project["pipeline"]["run"] | {"self_heal": False, "trace": "off"}
        passes = 0
        for _ in range(3):
            if stand:
                stand.reset()
            rep = await runner.run_test(test, headless, credentials=creds, cfg=clean_cfg, base_url=url)
            passes += rep["passed"]
        out.update(clean_passes=passes, first_try=passes == 3)
        if stand:
            stand.reset()
        rep = await runner.run_test(test, headless, credentials=creds, cfg=clean_cfg, hooks=DefectHooks(sc["defect"]),
                                    base_url=url)
        out["catches_defect"] = not rep["passed"]
        out["survives_v2"] = None
        if sc.get("v2") and stand:
            stand.reset("v2")
            rep = await runner.run_test(test, headless, credentials=creds, cfg=project["pipeline"]["run"] | {"trace": "off"},
                                        base_url=url)
            out["survives_v2"] = rep["passed"]
            out["healed_v2"] = rep["healed"]
            stand.reset()
        v = await mutations.verify(project, storage.load(test["id"]) or test)
        out["mutation_score"] = v.get("score")
        out["test_id"] = test["id"]
    out["cost"] = usage.as_dict()
    mark = "✔" if out["first_try"] else "~"
    log(f"  {mark} {site['id']}/{sc['id']}: 3/3={out['first_try']} дефект={out['catches_defect']} "
        f"v2={out['survives_v2']} мутанты={out['mutation_score']} {out['seconds']} с")
    return out


def _share(items: list, key: str):
    vals = [x[key] for x in items if x.get(key) is not None]
    return round(sum(bool(v) for v in vals) / len(vals), 3) if vals else None


def summarize(results: list[dict]) -> dict:
    done = [r for r in results if not r.get("skipped")]
    scores = [r["mutation_score"] for r in done if r.get("mutation_score") is not None]
    seconds = [r["seconds"] for r in done if r.get("authored")]
    costs_usd = [(r.get("cost") or {}).get("cost_usd") for r in done]
    costs_rub = [(r.get("cost") or {}).get("cost_rub") for r in done]
    return {"scenarios": len(done), "first_try": _share(done, "first_try"),
            "catches_defect": _share(done, "catches_defect"),
            "mutation_score": round(statistics.mean(scores), 3) if scores else None,
            "survives_v2": _share(done, "survives_v2"),
            "edits": round(statistics.mean(r.get("edits", 0) for r in done), 2) if done else None,
            "minutes": round(statistics.median(seconds) / 60, 1) if seconds else None,
            "cost_usd": round(sum(c for c in costs_usd if c) / len(done), 4) if done and all(c is not None for c in costs_usd) else None,
            "cost_rub": round(sum(c for c in costs_rub if c) / len(done), 2) if done and all(c is not None for c in costs_rub) else None}


def html_report(result: dict) -> str:
    s = result["summary"]
    def cell(v, target, lower=False):
        if v is None:
            return "<td>—</td>"
        ok = v <= target if lower else v >= target
        shown = f"{v:.0%}" if isinstance(v, float) and v <= 1 and not lower else v
        return f'<td class="{"ok" if ok else "bad"}">{shown}</td>'
    def mark(v):
        return "" if v is None else "✔" if v else "✘"

    rows = []
    for r in result["scenarios"]:
        score = r.get("mutation_score")
        rows.append(f"<tr><td>{html.escape(r['site'])}/{html.escape(r['id'])}</td><td>{mark(bool(r.get('first_try')))}</td>"
                    f"<td>{mark(r.get('catches_defect'))}</td><td>{mark(r.get('survives_v2'))}</td>"
                    f"<td>{'' if score is None else f'{score:.0%}'}</td><td>{r.get('seconds', '')}</td>"
                    f"<td>{html.escape(r.get('error') or r.get('skipped') or '')[:160]}</td></tr>")
    rows = "".join(rows)
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Бенчмарк {html.escape(result['model'])}</title>
<style>body{{font:14px system-ui,sans-serif;margin:24px;color:#1c2230}}table{{border-collapse:collapse;margin:12px 0}}
td,th{{border-bottom:1px solid #e3e6ec;padding:6px 10px;text-align:left}}.ok{{color:#2b8a3e;font-weight:600}}.bad{{color:#c92a2a;font-weight:600}}</style></head>
<body><h1>Бенчмарк: {html.escape(result['provider'])} / {html.escape(result['model'])}</h1>
<p>{html.escape(result['at'])} · сценариев: {s['scenarios']}</p>
<table><tr><th>Метрика</th><th>Значение</th><th>Ориентир</th></tr>
<tr><td>Рабочий тест с первой генерации (3 из 3)</td>{cell(s['first_try'], TARGETS['first_try'])}<td>≥ 70%</td></tr>
<tr><td>Ловит внесённый дефект</td>{cell(s['catches_defect'], TARGETS['catches_defect'])}<td>≥ 80%</td></tr>
<tr><td>Доля убитых мутантов</td>{cell(s['mutation_score'], TARGETS['mutation_score'])}<td>≥ 70%</td></tr>
<tr><td>Переживает смену вёрстки</td>{cell(s['survives_v2'], TARGETS['survives_v2'])}<td>≥ 85%</td></tr>
<tr><td>Правки человека на тест</td>{cell(s['edits'], TARGETS['edits'], lower=True)}<td>≤ 2</td></tr>
<tr><td>Время до готового теста, мин (медиана)</td>{cell(s['minutes'], TARGETS['minutes'], lower=True)}<td>≤ 10</td></tr>
<tr><td>Стоимость теста</td><td>{s['cost_usd'] if s['cost_usd'] is not None else '—'} $ / {s['cost_rub'] if s['cost_rub'] is not None else '—'} ₽</td><td>—</td></tr>
</table><h2>Сценарии</h2><table><tr><th>Сценарий</th><th>3/3</th><th>Дефект</th><th>v2</th><th>Мутанты</th><th>Сек</th><th>Ошибка</th></tr>{rows}</table>
</body></html>"""


async def run(provider: str = "", model: str = "", sites: list[str] | None = None, scenarios: list[str] | None = None,
              local_only: bool = False, limit: int = 0, headless: bool = True, out_dir: Path = RESULTS,
              log=print) -> dict:
    pid, model = providers.resolve({"provider": provider, "model": model})
    project = bench_project(pid, model)
    todo = [(site, sc) for site in load_sites() if (not sites or site["id"] in sites)
            and (not local_only or "{stand}" in site["url"]) for sc in site["scenarios"]
            if not scenarios or sc["id"] in scenarios]
    if limit:
        todo = todo[:limit]
    stand = _stand() if any("{stand}" in site["url"] for site, _ in todo) else None
    log(f"Бенчмарк {pid}/{model}: сценариев {len(todo)}")
    results = []
    try:
        for site, sc in todo:
            try:
                results.append(await run_scenario(project, site, sc, stand, headless, log))
            except Exception as e:
                results.append({"site": site["id"], "id": sc["id"], "first_try": False,
                                "error": f"{type(e).__name__}: {e}"[:300]})
                log(f"  ✘ {site['id']}/{sc['id']}: {e}")
    finally:
        if stand:
            stand.close()
    result = {"provider": pid, "model": model, "at": datetime.datetime.now().isoformat(timespec="seconds"),
              "scenarios": results, "summary": summarize(results)}
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.date.today().isoformat()}-{pid}-{model}".replace("/", "_").replace(":", "_")
    (out_dir / f"{stem}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    (out_dir / f"{stem}.html").write_text(html_report(result), "utf-8")
    if result["summary"]["first_try"] is not None and len(results) >= 5:
        providers.record_bench(pid, model, {"success": result["summary"]["first_try"], "at": time.time(),
                                            "scenarios": len(results), "summary": result["summary"]})
    log(json.dumps(result["summary"], ensure_ascii=False))
    log(f"Отчёт: {out_dir / (stem + '.html')}")
    return result


# ---------- comparing approaches (4.2) ----------

METRIC_TITLES = [("first_try", "Рабочий тест с первой генерации"), ("catches_defect", "Ловит внесённый дефект"),
                 ("mutation_score", "Доля убитых мутантов"), ("survives_v2", "Переживает смену вёрстки"),
                 ("edits", "Правки человека на тест"), ("minutes", "Время до готового теста, мин"),
                 ("cost_usd", "Стоимость теста, $")]


def compare(files: list[Path]) -> dict:
    """Results of the studio (bench JSON) and of other approaches (the same "summary" keys, e.g. a QA engineer
    writing by hand or Playwright Test Agents in Claude Code, measured on the same scenarios: see
    bench/external-template.json) side by side."""
    rows = []
    for f in files:
        d = json.loads(Path(f).read_text("utf-8"))
        name = d.get("approach") or f"{d.get('provider', '')}/{d.get('model', '')}"
        rows.append({"approach": name, "summary": d.get("summary") or {}})
    return {"approaches": rows}


def compare_html(cmp: dict) -> str:
    head = "".join(f"<th>{html.escape(r['approach'])}</th>" for r in cmp["approaches"])
    body = "".join(f"<tr><td>{t}</td>" + "".join(f"<td>{'—' if r['summary'].get(k) is None else r['summary'][k]}</td>"
                                                  for r in cmp["approaches"]) + "</tr>" for k, t in METRIC_TITLES)
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Сравнение подходов</title>'
            f'<style>body{{font:14px system-ui;margin:24px}}td,th{{border-bottom:1px solid #ddd;padding:6px 10px}}</style>'
            f"</head><body><h1>Сравнение подходов</h1><table><tr><th>Метрика</th>{head}</tr>{body}</table></body></html>")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["compare"]:
        ap = argparse.ArgumentParser(prog="python -m testgen.bench compare")
        ap.add_argument("files", nargs="+")
        ap.add_argument("--html")
        args = ap.parse_args(argv[1:])
        cmp = compare([Path(f) for f in args.files])
        for k, t in METRIC_TITLES:
            print(f"{t:40}" + "".join(f"{str(r['summary'].get(k, '—')):>14}" for r in cmp["approaches"]))
        if args.html:
            Path(args.html).write_text(compare_html(cmp), "utf-8")
        return 0
    ap = argparse.ArgumentParser(prog="python -m testgen.bench", description="Benchmark a model on bench/sites.json")
    ap.add_argument("--provider", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--site", action="append", default=[])
    ap.add_argument("--scenario", action="append", default=[])
    ap.add_argument("--local-only", action="store_true", help="only the local stand (no internet needed)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--out", default=str(RESULTS))
    args = ap.parse_args(argv)
    asyncio.run(run(args.provider, args.model, args.site, args.scenario, args.local_only, args.limit,
                    not args.headed, Path(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
