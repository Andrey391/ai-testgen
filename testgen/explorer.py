"""Planner: find test scenarios without a specification by exploring the site.

The explorer walks the application from its base URL, breadth first, within a
page and depth limit, and records every page: title, headings, forms with their
fields, buttons, links and a text excerpt. The map becomes plain-text
"requirements" that go into the usual scenarios.generate, so the rest of the
pipeline (authoring, runs, publishing) works as with a Jira ticket.

Only the given page and its child pages are explored: links of the same site
whose path lies under the start path (/catalog -> /catalog/42, not /admin); from
the site root that is the whole site. It only ever follows <a href> links with GET: no clicks, no
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

from . import fs, knowledge, llm, projects, storage, traffic
from .browser import BrowserSession
from .steps import needs_element, perform

SKIP = re.compile(r"log-?out|sign-?out|выход|выйти|delete|remove|destroy|удал|unsubscribe|отпис|"
                  r"deactivat|\.(pdf|zip|rar|7z|exe|msi|dmg|docx?|xlsx?|pptx?|csv|mp4|mp3)(\?|$)", re.I)
MAX_REQUIREMENTS = 60_000
MAP_TITLE = "# Карта приложения"
LIVE: dict[str, dict] = {}

PAGE_JS = r"""() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const FIELDS = 'input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea';
  // Texts the page already holds for its fields, shown or hidden: hints and error messages.
  const HINTS = 'small, .hint, .help, [class*=hint], [class*=help], [class*=description]';
  const ERRORS = '[role=alert], [aria-live], .error, .errors, .invalid-feedback, [class*=error], [class*=invalid], [data-error]';
  const text = el => { const t = clean(el.textContent).slice(0, 160); return /\p{L}{2}/u.test(t) ? t : ''; };
  const texts = els => [...new Set([...els].filter(e => !e.querySelector(FIELDS)).map(text).filter(Boolean))].slice(0, 6);
  const byIds = ids => (ids || '').split(/\s+/).map(i => i && document.getElementById(i)).filter(Boolean);
  // A caption next to the field when it has no <label>: the nearest preceding short text that holds no fields.
  const caption = el => {
    for (let node = el, up = 0; node && up < 3 && node.tagName !== 'FORM'; node = node.parentElement, up++)
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling) {
        if (s.matches(FIELDS) || s.querySelector(FIELDS) || s.matches('button')) break;
        if (s.matches(HINTS) || s.matches(ERRORS)) continue;
        const t = clean(s.innerText);
        if (t && t.length <= 60) return t;
      }
    return '';
  };
  const label = el => {
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const by = byIds(el.getAttribute('aria-labelledby')).map(l => clean(l.innerText)).join(' ');
    if (by) return by;
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
    const p = el.closest('label'); if (p) return clean(p.innerText);
    return caption(el) || clean(el.getAttribute('placeholder') || el.getAttribute('name') || '');
  };
  const field = el => {
    const name = label(el), attr = a => el.getAttribute(a) || '';
    const box = el.parentElement && el.parentElement.querySelectorAll(FIELDS).length === 1 ? el.parentElement : null;
    const described = byIds(attr('aria-describedby'));
    // Messages declared on the field itself (data-val-required, data-msg-…, data-error-…: ASP.NET, jQuery Validate…).
    const declared = [...el.attributes].filter(a => /^data-(val-|msg|error|message|invalid)/i.test(a.name)
      && /\p{L}{3}/u.test(a.value) && a.value.length > 5).map(a => clean(a.value).slice(0, 160));
    const errors = [...new Set([...declared, ...texts(byIds(attr('aria-errormessage'))),
      ...texts(described.filter(d => d.matches(ERRORS))), ...(box ? texts(box.querySelectorAll(ERRORS)) : [])])];
    const hints = texts([...described.filter(d => !d.matches(ERRORS)), ...(box ? box.querySelectorAll(HINTS) : [])])
      .filter(h => !errors.includes(h) && h !== name);
    const options = el.tagName === 'SELECT' ? [...el.options].map(o => clean(o.text)).filter(o => o && !/^[-—–.\s]*$/.test(o)) : [];
    return {label: name.replace(/\s*\*$/, '').slice(0, 80), name: attr('name'),
      type: el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : (attr('type') || 'text'),
      required: el.required || attr('aria-required') === 'true' || /\*$/.test(name),
      options: options.slice(0, 30), more_options: Math.max(0, options.length - 30),
      placeholder: attr('placeholder') !== name ? attr('placeholder').slice(0, 80) : '',
      minlength: el.minLength > 0 ? el.minLength : null, maxlength: el.maxLength >= 0 ? el.maxLength : null,
      pattern: attr('pattern').slice(0, 120), title: attr('title').slice(0, 160),
      min: attr('min'), max: attr('max'), step: attr('step'), inputmode: attr('inputmode'),
      hints, errors};
  };
  const forms = [...document.querySelectorAll('form')].filter(visible).slice(0, 10).map(f => {
    const fields = [...f.querySelectorAll(FIELDS)].filter(visible).slice(0, 25).map(field);
    const own = new Set(fields.flatMap(x => x.errors));
    return {name: clean(f.getAttribute('aria-label') || f.getAttribute('name') || f.id || ''), fields,
      submit: [...f.querySelectorAll('button, input[type=submit]')].filter(visible)
        .map(b => clean(b.innerText || b.value)).filter(Boolean).slice(0, 3),
      novalidate: f.noValidate, errors: texts(f.querySelectorAll(ERRORS)).filter(e => !own.has(e))};
  });
  const loose = [...document.querySelectorAll(FIELDS)].filter(el => !el.closest('form') && visible(el)).slice(0, 25).map(field);
  if (loose.length) forms.push({name: '', fields: loose, submit: [], novalidate: false, errors: []});
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


def _in_scope(url: str, start: str) -> bool:
    """The start page or one of its child pages: the same site, the path under the start path
    (a file such as /app/index.html stands for its folder /app)."""
    if not _same_site(url, start):
        return False
    base = urlparse(start).path.rstrip("/")
    if "." in base.rsplit("/", 1)[-1]:
        base = base.rsplit("/", 1)[0]
    path = urlparse(url).path.rstrip("/")
    return not base or path == base or path.startswith(base + "/")


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


async def learn_model(project: dict, result: dict, log=None) -> None:
    """The application model learns from the map: entities, dependencies, lifecycles, roles and the
    data the pages show (the setting "requirements.learn_model"). A help: the map does not wait for it."""
    if not result["pages"] or not project["pipeline"]["requirements"].get("learn_model"):
        return
    if log:
        log("Модель приложения: сущности, зависимости и тестовые данные по карте сайта…")
    try:
        doc = await knowledge.extract(project, to_requirements(result), source="explore")
        if log:
            log(f"Модель приложения обновлена: сущностей {len(doc['entities'])}, тестовых данных {len(doc['data'])}")
    except Exception as e:
        if log:
            log(f"Модель приложения не обновлена: {llm.api_error_text(e)}")


async def explore(project: dict, url: str = "", log=None, state: dict | None = None, learn: bool = False) -> dict:
    """Crawl the start page and its child pages -> the map (also saved as the project's latest map). `learn`: then the
    application model learns from it (learn_model)."""
    cfg = project["pipeline"]["explore"]
    start = _norm(url or project.get("base_url") or "")
    if not start.startswith("http"):
        raise ValueError("Не указан URL приложения для исследования")
    result = state if state is not None else {}
    result.update({"id": result.get("id") or uuid.uuid4().hex[:10], "project_id": project["id"],
                   "start": start, "at": time.time(), "status": "running", "pages": [], "skipped": 0,
                   "error": ""})
    # The requests the pages make go into the API catalog (traffic.catalog): endpoints for API tests.
    bs = await BrowserSession.launch(headless=cfg["headless"], record_traffic=True)
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
            if not _in_scope(link, start) or SKIP.search(link):
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
    except Exception as e:
        result.update(status="error", error=str(e).splitlines()[0][:300])
    finally:
        result["api"] = traffic.endpoints(bs.traffic or [], start)
        await bs.close()
    if result["status"] == "running":
        if learn:
            await learn_model(project, result, log)
        result["status"] = "done"
    result["finished"] = time.time()
    save(result)
    return result


def save(result: dict) -> None:
    d = _dir(result["project_id"])
    text = json.dumps(result, ensure_ascii=False, indent=1)
    fs.write_text(d / f"{result['id']}.json", text)
    if result["status"] == "done":
        fs.write_text(d / "latest.json", text)


def latest(pid: str) -> dict | None:
    return fs.read_json(_dir(pid) / "latest.json")


def get(pid: str, eid: str) -> dict | None:
    if eid in LIVE:
        return LIVE[eid]
    if not re.fullmatch(r"[0-9a-f]{10}", eid or ""):
        return None
    return fs.read_json(_dir(pid) / f"{eid}.json")


def _quoted(items) -> str:
    return ", ".join(f"«{t}»" for t in items)


def _field_text(x: dict) -> str:
    """A field of the map: its type and what the page declares about it - required, length, format, range,
    the hint and the error texts the page already holds."""
    parts = [x["type"]]
    if x["required"]:
        parts.append("обязательное")
    lo, hi = x.get("minlength"), x.get("maxlength")
    if lo or hi is not None:
        parts.append(f"длина {lo}–{hi}" if lo and hi is not None else f"длина от {lo}" if lo else f"длина до {hi}")
    if x.get("pattern"):
        parts.append(f"формат: {x['pattern']}")
    if x.get("min") or x.get("max"):
        parts.append(f"диапазон: {x.get('min') or '…'}–{x.get('max') or '…'}" + (f", шаг {x['step']}" if x.get("step") else ""))
    if x.get("inputmode"):
        parts.append(f"ввод: {x['inputmode']}")
    if x["options"]:
        parts.append(f"варианты: {'/'.join(x['options'])}" + (f" и ещё {x['more_options']}" if x.get("more_options") else ""))
    hints = [h for h in [x.get("title"), x.get("placeholder")] if h] + list(x.get("hints") or [])
    if hints:
        parts.append(f"подсказка: {_quoted(dict.fromkeys(hints))}")
    if x.get("errors"):
        parts.append(f"ошибки: {_quoted(x['errors'])}")
    return f"{x['label'] or x['name'] or x['type']} ({', '.join(parts)})"


def to_requirements(result: dict) -> str:
    """The map as requirements text for scenarios.generate."""
    out = [f"{MAP_TITLE} {result['start']}",
           "Требования восстановлены автоматическим обходом интерфейса (Planner), ТЗ нет. Ожидаемое поведение "
           "выводи из интерфейса и общепринятых правил для таких страниц и форм; не придумывай функции, которых "
           "на страницах нет. Сценарии не должны выполнять необратимых действий (оплата, удаление, отправка).",
           "У полей форм указано то, что объявляет страница: обязательность, длина, формат, диапазон, подсказки и "
           "тексты ошибок, уже заложенные в разметку. Чего страница не объявляет (правила сервера, тексты ошибок, "
           "которые появляются только после отправки, переходы), обходом не узнать: негативный сценарий проверяет, "
           "что форма не принята и показана ошибка, а точный текст записывает агент, увидев его на стенде."]
    for p in result["pages"]:
        if p.get("error"):
            continue
        path = urlparse(p["url"]).path or "/"
        block = [f"\n## {p.get('title') or path} — {path}"]
        if p.get("headings"):
            block.append("Заголовки: " + " · ".join(p["headings"]))
        for f in p.get("forms") or []:
            fields = ", ".join(_field_text(x) for x in f["fields"])
            name = f["name"] or (f["submit"][0] if f["submit"] else "")
            block.append(f"Форма{f' «{name}»' if name else ''}: {fields or 'без полей'}"
                         + (f"; кнопки: {', '.join(f['submit'])}" if f["submit"] else "")
                         + ("; проверка полей браузером выключена (novalidate)" if f.get("novalidate") else "")
                         + (f"; сообщения формы: {_quoted(f['errors'])}" if f.get("errors") else ""))
        if p.get("buttons"):
            block.append("Кнопки: " + ", ".join(p["buttons"]))
        targets = sorted({urlparse(a["href"]).path or "/" for a in p.get("links", [])
                          if _in_scope(a["href"], result["start"])} - {path})
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


def _short(items: list[str], n: int) -> str:
    items = [x for x in dict.fromkeys(items) if x]
    return ", ".join(items[:n]) + (f" и ещё {len(items) - n}" if len(items) > n else "")


def summary(p: dict, common_title: str = "") -> str:
    """A one-line description of a mapped page: what it shows and what can be done on it."""
    parts = []
    title = (p.get("title") or "").strip()
    heads = [h for h in p.get("headings") or [] if h != title and h != common_title]
    if title and title != common_title:
        parts.append(title)
    if heads:
        parts.append(_short(heads, 2))
    for f in p.get("forms") or []:
        fields = _short([x["label"] or x["name"] or x["type"] for x in f["fields"]], 4)
        name = f["name"] or (f["submit"][0] if f["submit"] else "")
        if fields or name:
            parts.append(f"форма{f' «{name}»' if name else ''}" + (f": {fields}" if fields else ""))
    if p.get("buttons"):
        parts.append("кнопки: " + _short(p["buttons"], 3))
    if not parts and p.get("text"):
        parts.append(p["text"][:120] + ("…" if len(p["text"]) > 120 else ""))
    return " · ".join(parts)


def coverage(pid: str) -> dict | None:
    """Pages of the latest map and the tests that visit them."""
    m = latest(pid)
    if not m:
        return None
    tests = [(t["name"], _paths_of(t)) for t in storage.all_tests(pid)]
    live = [p for p in m["pages"] if not p.get("error")]
    titles = [p.get("title") or "" for p in live]
    # the site name repeated on every page says nothing about the page itself
    common = max(set(titles), key=titles.count) if titles else ""
    common = common if len(live) > 1 and titles.count(common) > len(live) / 2 else ""
    pages = []
    for p in live:
        path = urlparse(p["url"]).path.rstrip("/") or "/"
        by = [name for name, paths in tests if path in paths]
        pages.append({"url": p["url"], "path": path, "title": p.get("title", ""), "tests": by,
                      "forms": len(p.get("forms") or []), "summary": summary(p, common)})
    return {"at": m["at"], "start": m["start"], "pages": pages, "total": len(pages),
            "covered": sum(bool(p["tests"]) for p in pages), "site_title": common}
