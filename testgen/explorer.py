"""Planner: find test scenarios without a specification by exploring the site.

The explorer walks the application from its base URL, breadth first, within a
page and depth limit, and records every page: title, headings, forms with their
fields, buttons, links and a text excerpt. The map becomes plain-text
"requirements" that go into the usual scenarios.generate, so the rest of the
pipeline (authoring, runs, publishing) works as with a Jira ticket.

It only ever follows <a href> links with GET on the same site: no clicks, no
form submissions, and links that look like they change state (log out, delete,
unsubscribe...) or download files are skipped - the same "no irreversible
actions" rule the authoring agent follows. To explore behind a login, the
project names a saved test (explore.login_test) that is replayed first.

Maps are kept in data/projects/<id>/explore/; the latest one also gives the
screen coverage: which pages no saved test visits.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from urllib.parse import urldefrag, urlparse

from . import projects, storage
from .browser import BrowserSession
from .steps import needs_element, perform

SKIP = re.compile(r"log-?out|sign-?out|выход|выйти|delete|remove|destroy|удал|unsubscribe|отпис|"
                  r"deactivat|\.(pdf|zip|rar|7z|exe|msi|dmg|docx?|xlsx?|pptx?|csv|mp4|mp3)(\?|$)", re.I)
MAX_REQUIREMENTS = 60_000
LIVE: dict[str, dict] = {}

PAGE_JS = r"""() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const label = el => {
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
    const p = el.closest('label'); if (p) return clean(p.innerText);
    return clean(el.getAttribute('placeholder') || el.getAttribute('name') || '');
  };
  const field = el => ({label: label(el).slice(0, 80), name: el.getAttribute('name') || '',
    type: el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : (el.getAttribute('type') || 'text'),
    required: el.required || el.getAttribute('aria-required') === 'true',
    options: el.tagName === 'SELECT' ? [...el.options].slice(0, 10).map(o => clean(o.text)) : []});
  const FIELDS = 'input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea';
  const forms = [...document.querySelectorAll('form')].filter(visible).slice(0, 10).map(f => ({
    name: clean(f.getAttribute('aria-label') || f.getAttribute('name') || f.id || ''),
    fields: [...f.querySelectorAll(FIELDS)].filter(visible).slice(0, 25).map(field),
    submit: [...f.querySelectorAll('button, input[type=submit]')].filter(visible)
      .map(b => clean(b.innerText || b.value)).filter(Boolean).slice(0, 3)}));
  const loose = [...document.querySelectorAll(FIELDS)].filter(el => !el.closest('form') && visible(el)).slice(0, 25).map(field);
  if (loose.length) forms.push({name: '', fields: loose, submit: []});
  const buttons = [...document.querySelectorAll('button, [role=button], input[type=button]')]
    .filter(b => visible(b) && !b.closest('form')).map(b => clean(b.innerText || b.value || b.getAttribute('aria-label')))
    .filter(Boolean);
  const links = [...document.querySelectorAll('a[href]')].filter(visible)
    .map(a => ({href: a.href, text: clean(a.innerText || a.getAttribute('aria-label')).slice(0, 60)}))
    .filter(l => /^https?:/.test(l.href));
  return {title: document.title, headings: [...document.querySelectorAll('h1, h2')].filter(visible)
      .map(h => clean(h.innerText)).filter(Boolean).slice(0, 10),
    forms, buttons: [...new Set(buttons)].slice(0, 20), links: links.slice(0, 150),
    text: clean(document.body ? document.body.innerText : '').slice(0, 600)};
}"""


def _norm(url: str) -> str:
    url = urldefrag(url)[0]
    u = urlparse(url)
    return u._replace(path=u.path.rstrip("/") or "/").geturl()


def _same_site(url: str, start: str) -> bool:
    return urlparse(url).netloc == urlparse(start).netloc


def _dir(pid: str):
    return projects.path(pid) / "explore"


def find_test(pid: str, ref: str) -> dict | None:
    """A saved test of the project by id or by name."""
    if not ref:
        return None
    t = storage.load(ref)
    if t and t["project_id"] == pid:
        return t
    return next((t for t in storage.all_tests(pid) if t["name"].strip().lower() == ref.strip().lower()), None)


async def _login(bs: BrowserSession, test: dict) -> None:
    for i, step in enumerate(test["steps"]):
        bs.step_index = i
        loc = None
        if needs_element(step):
            await bs.settle()
            loc, _ = await bs.find(step["locator"], wait=5)
            if loc is None:
                raise RuntimeError(f"Вход не выполнен: не найден элемент шага «{step['description']}»")
        await perform(bs, step, loc)


async def explore(project: dict, url: str = "", log=None, state: dict | None = None) -> dict:
    """Crawl the site -> the map (also saved as the project's latest map)."""
    cfg = project["pipeline"]["explore"]
    start = _norm(url or project.get("base_url") or "")
    if not start.startswith("http"):
        raise ValueError("Не указан URL приложения для исследования")
    result = state if state is not None else {}
    result.update({"id": result.get("id") or uuid.uuid4().hex[:10], "project_id": project["id"],
                   "start": start, "at": time.time(), "status": "running", "pages": [], "skipped": 0,
                   "error": ""})
    bs = await BrowserSession.launch(headless=cfg["headless"])
    try:
        login = find_test(project["id"], cfg.get("login_test", ""))
        if login:
            bs.credentials = storage.credentials(login)
            if log:
                log(f"Вход: повтор теста «{login['name']}»")
            await _login(bs, login)
        queue, seen = [(start, 0)], set()
        while queue and len(result["pages"]) < cfg["max_pages"]:
            link, depth = queue.pop(0)
            link = _norm(link)
            if link in seen:
                continue
            seen.add(link)
            if not _same_site(link, start) or SKIP.search(link):
                result["skipped"] += 1
                continue
            page = {"url": link, "depth": depth}
            try:
                resp = await bs.page.goto(link, wait_until="domcontentloaded", timeout=30000)
                await bs.settle()
                page |= await bs.page.evaluate(PAGE_JS)
                page["url"], page["status"] = _norm(bs.url), resp.status if resp else None
            except Exception as e:
                page["error"] = str(e).splitlines()[0][:200]
            if any(p["url"] == page["url"] for p in result["pages"]):
                continue        # a redirect to a page we already have
            result["pages"].append(page)
            if log:
                log(f"Страница {len(result['pages'])}: {page.get('title') or page['url']}")
            if depth < cfg["max_depth"]:
                for a in page.get("links", []):
                    if SKIP.search(a["text"]):
                        result["skipped"] += 1
                    elif _norm(a["href"]) not in seen:
                        queue.append((a["href"], depth + 1))
        result["status"] = "done"
    except Exception as e:
        result.update(status="error", error=str(e).splitlines()[0][:300])
    finally:
        await bs.close()
    result["finished"] = time.time()
    save(result)
    return result


def save(result: dict) -> None:
    d = _dir(result["project_id"])
    d.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    (d / f"{result['id']}.json").write_text(text, "utf-8")
    if result["status"] == "done":
        (d / "latest.json").write_text(text, "utf-8")


def latest(pid: str) -> dict | None:
    f = _dir(pid) / "latest.json"
    return json.loads(f.read_text("utf-8")) if f.exists() else None


def get(pid: str, eid: str) -> dict | None:
    if eid in LIVE:
        return LIVE[eid]
    if not re.fullmatch(r"[0-9a-f]{10}", eid or ""):
        return None
    f = _dir(pid) / f"{eid}.json"
    return json.loads(f.read_text("utf-8")) if f.exists() else None


def to_requirements(result: dict) -> str:
    """The map as requirements text for scenarios.generate."""
    out = [f"# Карта приложения {result['start']}",
           "Требования восстановлены автоматическим обходом интерфейса (Planner), ТЗ нет. Ожидаемое поведение "
           "выводи из интерфейса и общепринятых правил для таких страниц и форм; не придумывай функции, которых "
           "на страницах нет. Сценарии не должны выполнять необратимых действий (оплата, удаление, отправка)."]
    for p in result["pages"]:
        if p.get("error"):
            continue
        path = urlparse(p["url"]).path or "/"
        block = [f"\n## {p.get('title') or path} — {path}"]
        if p.get("headings"):
            block.append("Заголовки: " + " · ".join(p["headings"]))
        for f in p.get("forms") or []:
            fields = ", ".join(f"{x['label'] or x['name'] or x['type']} ({x['type']}"
                               + (", обязательное" if x["required"] else "")
                               + (f", варианты: {'/'.join(x['options'])}" if x["options"] else "") + ")"
                               for x in f["fields"])
            name = f["name"] or (f["submit"][0] if f["submit"] else "")
            block.append(f"Форма{f' «{name}»' if name else ''}: {fields or 'без полей'}"
                         + (f"; кнопки: {', '.join(f['submit'])}" if f["submit"] else ""))
        if p.get("buttons"):
            block.append("Кнопки: " + ", ".join(p["buttons"]))
        targets = sorted({urlparse(a["href"]).path or "/" for a in p.get("links", [])
                          if _same_site(a["href"], result["start"])} - {path})
        if targets:
            block.append("Ссылки на: " + ", ".join(targets[:30]))
        if p.get("text"):
            block.append(f"Текст: {p['text'][:400]}")
        out.append("\n".join(block))
    return "\n".join(out)[:MAX_REQUIREMENTS]


def _paths_of(test: dict) -> set[str]:
    paths = {urlparse(s["value"]).path.rstrip("/") or "/" for s in test["steps"]
             if s["action"] == "navigate" and s.get("value", "").startswith("http")}
    paths |= {p.rstrip("/") or "/" for p in (test.get("last_run") or {}).get("paths") or []}
    return paths


def coverage(pid: str) -> dict | None:
    """Pages of the latest map and the tests that visit them."""
    m = latest(pid)
    if not m:
        return None
    tests = [(t["name"], _paths_of(t)) for t in storage.all_tests(pid)]
    pages = []
    for p in m["pages"]:
        if p.get("error"):
            continue
        path = urlparse(p["url"]).path.rstrip("/") or "/"
        by = [name for name, paths in tests if path in paths]
        pages.append({"url": p["url"], "path": path, "title": p.get("title", ""), "tests": by,
                      "forms": len(p.get("forms") or [])})
    return {"at": m["at"], "start": m["start"], "pages": pages, "total": len(pages),
            "covered": sum(bool(p["tests"]) for p in pages)}
