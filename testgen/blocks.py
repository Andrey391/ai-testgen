"""Reused steps: chains of actions that tests share run as modules, never as copies.

A test is a source of reusable steps when it is stable (its last run passed, not in quarantine) and a
UI test. Only actions are reused (REUSABLE): assertions, waits and steps that need the scenario's own
context (API requests, mocks, mail, dialogs, modules themselves) never go into a block - a module is
"how to get there", the checks stay with each test.

- offer(pid, text): what the agent may reuse while authoring a scenario - the project's modules and the
  runs of actions of stable tests, narrowed to the sections of the scenario (the groups of the
  application model it names, the words of its title) and to the start page. reuse() turns the chosen
  run into a module and puts a use_module step in its place in the source test: the steps live in one
  place, a fix (or a heal) of the module serves every test.
- candidates(pid): chains of actions repeated in several stable tests (values may differ: they become
  parameters), offered to a person in «Тесты → Повторяющиеся шаги», grouped by section; accept() makes
  the module and replaces every occurrence, dismiss() stops offering the chain
  (data/projects/<id>/blocks.json).

A module's `section` (groups of the model, else the first segment of its start page) and `start_path`
(the path template of the page it starts on, "" - it opens its page itself) narrow what the agent sees:
the modules of the scenario's sections at the start, the modules of the current page in every page state.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections import defaultdict
from urllib.parse import urlparse

from . import fs, knowledge, projects, storage
from .steps import new_step

# Actions a block may contain: what a user does, nothing that checks or depends on the scenario's data flow.
REUSABLE = {"navigate", "click", "double_click", "fill", "select_option", "hover", "press_key", "scroll",
            "upload_file", "drag_to", "switch_tab"}
VALUED = {"fill", "select_option", "navigate", "upload_file", "press_key"}   # a value that may become a parameter
MIN_CHAIN = 2           # actions in a run the agent may reuse
MIN_REPEAT = 3          # actions in a repeated chain offered as a module
MAX_CHAIN = 15
MAX_TESTS = 6           # stable tests whose runs the agent sees
MAX_MODULES = 15        # modules listed to the agent (the rest: those of the current page)
MAX_RUNS = 12           # runs of actions listed to the agent
_ID = re.compile(r"^(\d+|[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f-]{27,})$", re.I)
_WORD = re.compile(r"[\wё]{4,}", re.I)


def _doc_path(pid: str):
    return projects.path(pid) / "blocks.json"


def reusable(step: dict) -> bool:
    return step.get("action") in REUSABLE


def stable(test: dict) -> bool:
    return (test.get("role") != "module" and (test.get("layer") or "ui") != "api"
            and (test.get("last_run") or {}).get("status") == "passed"
            and not (test.get("quarantine") or {}).get("on"))


def path_key(url: str) -> str:
    """The page's path template: ids (numbers, hex, uuids) become {id}, no query, no trailing slash."""
    segs = [("{id}" if _ID.match(s) else s.lower()) for s in urlparse(url or "").path.split("/") if s]
    return "/" + "/".join(segs)


def page_of(test: dict, i: int) -> str:
    """The page step i of a test starts on: recorded while authoring (`page`), else the last navigation."""
    steps = test.get("steps") or []
    if i < len(steps) and steps[i].get("page"):
        return steps[i]["page"]
    for s in reversed(steps[:i]):
        if s.get("action") == "navigate" and s.get("value"):
            return s["value"]
    return test.get("url") or ""


def start_path(test: dict, i: int) -> str:
    """Where a chain starting at step i must start: "" when it opens its page itself."""
    steps = test.get("steps") or []
    if i < len(steps) and steps[i].get("action") == "navigate":
        return ""
    return path_key(page_of(test, i))


def runs(test: dict, min_len: int = MIN_CHAIN) -> list[tuple[int, int]]:
    """The maximal runs of reusable actions of a test: [(start, end)] (end exclusive)."""
    out, start = [], None
    steps = test.get("steps") or []
    for i, s in enumerate(steps + [{}]):
        if i < len(steps) and reusable(s):
            start = i if start is None else start
            continue
        if start is not None and i - start >= min_len:
            out.append((start, i))
        start = None
    return out


def _locator(step: dict) -> str:
    loc = step.get("locator") or []
    return json.dumps(loc[0], sort_keys=True, ensure_ascii=False) if loc else ""


def sig(step: dict, loose: bool = False) -> tuple:
    """What makes two steps the same: the action, the element and the value (`loose`: values that may
    become parameters are not compared; a navigation compares its page)."""
    a = step.get("action", "")
    v = step.get("value") or ""
    if a == "navigate":
        v = path_key(v)
    elif loose and a in VALUED:
        v = "*"
    return a, _locator(step), v


def _key(chain: tuple) -> str:
    return hashlib.sha1(json.dumps(chain, ensure_ascii=False).encode()).hexdigest()[:12]


def _inside(small: tuple, big: tuple) -> bool:
    n = len(small)
    return any(big[i:i + n] == small for i in range(len(big) - n + 1))


def _words(text: str) -> set[str]:
    return {w.lower().replace("ё", "е")[:6] for w in _WORD.findall(text or "")}


def sections(pid: str, text: str, doc: dict | None = None) -> list[str]:
    """The sections of a text: the groups of the application model's entities it names."""
    try:
        units = knowledge.units_for(pid, text, doc)
    except Exception:
        return []
    return [u for u in units or [] if u and u != knowledge.ROLES_UNIT]


def section_label(secs: list[str], path: str) -> str:
    if secs:
        return secs[0]
    first = [s for s in (path or "").split("/") if s and s != "{id}"]
    return "/" + first[0] if first else ""


def _test_text(test: dict) -> str:
    return f"{test.get('name', '')}\n{test.get('scenario', '')}"


def _step_line(i: int, s: dict) -> str:
    return f"{i}. [{s['action']}] {s.get('description', '')}" + (f" | value: {s['value']}" if s.get("value") else "")


def modules(pid: str) -> list[dict]:
    return [t for t in storage.all_tests(pid) if t.get("role") == "module"]


def module_start(m: dict) -> str:
    return m["start_path"] if "start_path" in m else start_path(m, 0)


# ---------- what the agent may reuse ----------

def offer(pid: str, text: str, exclude: tuple = ()) -> dict:
    """{"modules": [...], "chains": [{id, test_id, name, start, steps}]} for a scenario `text`:
    modules of its sections (all of them when there are few) and the runs of actions of the stable
    tests closest to it (same sections, words of the title)."""
    try:
        doc = knowledge.get(pid)
    except Exception:
        doc = None
    want = set(sections(pid, text, doc)) if doc else set()
    words = _words(text)
    mods = modules(pid)
    if len(mods) > MAX_MODULES:
        mods = [m for m in mods if set(m.get("section") or []) & want]
    known = {tuple(sig(s) for s in m.get("steps") or []) for m in modules(pid)}
    scored = []
    for t in storage.all_tests(pid):
        if t["id"] in exclude or not stable(t):
            continue
        rs = runs(t)
        if not rs:
            continue
        secs = set(sections(pid, _test_text(t), doc)) if doc and want else set()
        score = 3 * len(want & secs) + len(words & _words(_test_text(t)))
        if score > 0:
            scored.append((score, t, rs))
    scored.sort(key=lambda x: -x[0])
    chains = []
    for _, t, rs in scored[:MAX_TESTS]:
        for a, b in rs:
            b = min(b, a + MAX_CHAIN)
            steps = t["steps"][a:b]
            if tuple(sig(s) for s in steps) in known:
                continue            # already a module: the agent uses the module
            chains.append({"id": f"{t['id']}:{a + 1}-{b}", "test_id": t["id"], "name": t["name"],
                           "start": start_path(t, a), "steps": [_step_line(a + 1 + i, s) for i, s in enumerate(steps)]})
    return {"modules": mods, "chains": chains[:MAX_RUNS]}


def note(found: dict) -> str:
    """The reusable steps of the scenario for the agent's task."""
    parts = []
    if found["chains"]:
        lines = []
        for c in found["chains"]:
            where = f"starts on {c['start']}" if c["start"] else "opens its page itself"
            lines.append(f"- {c['id']} (test «{c['name']}», {where}):\n  " + "\n  ".join(c["steps"]))
        parts.append("Actions already recorded in stable tests of the project. Before recording actions yourself, look "
                     "here: when a run (or its first N actions) is EXACTLY what the scenario needs now, from the current "
                     "page, call reuse_steps with its id (and `count` for the first N) - it runs as ONE step and becomes "
                     "a module shared with that test instead of a copy. Assertions are never part of it: add them "
                     "yourself after it.\n" + "\n".join(lines))
    return "\n\n".join(parts)


def chain(pid: str, cid: str, count: int = 0) -> tuple[dict, int, int]:
    """The run `cid` ("<test id>:<from>-<to>", 1-based, inclusive) -> (test, start, end); ValueError if it
    is not a run of reusable actions of a stable test of the project."""
    try:
        tid, rng = str(cid).rsplit(":", 1)
        a, b = (int(x) for x in rng.split("-"))
    except ValueError:
        raise ValueError(f"Unknown run {cid!r}: use an id from the task, e.g. 'abc123:1-4'.") from None
    t = storage.load(tid)
    if not t or t.get("project_id") != pid or not stable(t):
        raise ValueError(f"Unknown run {cid!r}: the test is not a stable test of the project.")
    start, end = a - 1, b
    if count:
        end = min(end, start + int(count))
    if not any(s <= start and end <= e for s, e in runs(t)) or end - start < 1:
        raise ValueError(f"Run {cid!r} is not a run of actions of that test (assertions are never reused).")
    return t, start, end


# ---------- modules ----------

def _param_name(step: dict, used: set[str]) -> str:
    loc = (step.get("locator") or [{}])[0]
    base = re.sub(r"\W+", "_", str(loc.get("name") or loc.get("value") or "").lower(), flags=re.U).strip("_")[:24]
    base = base if base and not base[0].isdigit() else "value"
    name, n = base, 2
    while name in used:
        name, n = f"{base}_{n}", n + 1
    used.add(name)
    return name


def make_module(pid: str, name: str, source: dict, start: int, end: int,
                values: dict[str, list[str]] | None = None, tests: list[str] | None = None) -> dict:
    """A module of the steps source[start:end] (fresh ids, saved). `values` {test id: values of the chain's
    steps in that test}: a position where they differ becomes a {{params.x}} parameter."""
    steps = [copy.deepcopy(s) for s in source["steps"][start:end]]
    values = values or {}
    used: set[str] = set()
    for i, s in enumerate(steps):
        s.update(id=uuid.uuid4().hex[:8], status="passed", error="", healed=False)
        s.pop("page", None)
        if s["action"] in VALUED and len({v[i] for v in values.values()}) > 1:
            s["value"] = "{{params.%s}}" % _param_name(s, used)
    tests = tests or [source["id"]]
    module = {"id": uuid.uuid4().hex[:10], "project_id": pid, "name": name[:120], "url": page_of(source, start),
              "scenario": "Модуль: общие действия тестов проекта", "engine": "builtin", "layer": "ui",
              "role": "module", "status": "ready", "steps": steps,
              "section": sections(pid, "\n".join([name] + [s.get("description", "") for s in steps])),
              "start_path": start_path(source, start), "block": {"tests": tests}}
    return storage.save(module)


def params_of(module: dict, values: list[str]) -> dict:
    """The parameters of a module for one occurrence: its values at the parameter positions."""
    out = {}
    for i, s in enumerate(module["steps"]):
        m = re.fullmatch(r"\{\{params\.(\w+)\}\}", s.get("value") or "")
        if m and i < len(values):
            out[m.group(1)] = values[i]
    return out


def reuse(pid: str, source: dict, start: int, end: int, name: str) -> dict:
    """The run source[start:end] the agent reused: a module of it, and the source test calls the module
    in its place (the steps stay in one place)."""
    ids = [s["id"] for s in source["steps"][start:end]]
    module = make_module(pid, name, source, start, end, None, [source["id"]])
    replace(source["id"], ids, module)
    return module


def use_step(module: dict, params: dict | None = None) -> dict:
    step = new_step("use_module", f"Выполнить модуль «{module['name']}»",
                    json.dumps({"module": module["id"], "params": params or {}}, ensure_ascii=False), source="system")
    step["status"] = "passed"
    return step


def replace(tid: str, ids: list[str], module: dict, params: dict | None = None) -> bool:
    """Steps `ids` (in a row) of a test become one use_module step (a new version of the test)."""
    done = []

    def change(t: dict) -> None:
        order = [s["id"] for s in t["steps"]]
        if not ids or ids[0] not in order:
            return
        i = order.index(ids[0])
        if order[i:i + len(ids)] != ids:
            return
        t["steps"][i:i + len(ids)] = [use_step(module, params)]
        t["heal_proposals"] = [p for p in t.get("heal_proposals") or [] if p.get("step_id") not in ids]
        done.append(True)
    storage.update(tid, change)
    return bool(done)


# ---------- repeated chains offered to a person ----------

def _dismissed(pid: str) -> set[str]:
    return set((fs.read_json(_doc_path(pid), {}) or {}).get("dismissed") or [])


def _occurrences(test: dict, c: tuple) -> list[int]:
    """Starts of the non-overlapping occurrences of chain `c` (loose signatures) among the actions of a test."""
    steps = test.get("steps") or []
    loose = [sig(s, loose=True) for s in steps]
    out, i, n = [], 0, len(c)
    while i <= len(loose) - n:
        if tuple(loose[i:i + n]) == c and all(reusable(s) for s in steps[i:i + n]):
            out.append(i)
            i += n
        else:
            i += 1
    return out


def _repeated(pid: str) -> tuple[list[tuple[tuple, list[str]]], dict[str, dict]]:
    """([(chain, ids of the tests it is in)], stable tests by id): chains of at least MIN_REPEAT actions in
    two or more stable tests, longest first; a chain inside a longer one found in the same tests is left out,
    and so is one that is (inside) a module already or was dismissed."""
    tests = {t["id"]: t for t in storage.all_tests(pid) if stable(t)}
    occ: dict[tuple, set[str]] = defaultdict(set)
    for t in tests.values():
        for a, b in runs(t, MIN_REPEAT):
            loose = [sig(s, loose=True) for s in t["steps"][a:b]]
            for n in range(MIN_REPEAT, min(MAX_CHAIN, b - a) + 1):
                for i in range(len(loose) - n + 1):
                    occ[tuple(loose[i:i + n])].add(t["id"])
    repeated = sorted(((c, o) for c, o in occ.items() if len(o) >= 2), key=lambda x: (-len(x[0]), -len(x[1])))
    known = [tuple(sig(s, loose=True) for s in m.get("steps") or []) for m in modules(pid)]
    dismissed = _dismissed(pid)
    chosen: list[tuple[tuple, set[str]]] = []
    covered: list[tuple[tuple, set[str]]] = []      # chosen or dismissed: their parts in the same tests are not offered
    for c, o in repeated:
        if any(_inside(c, k) for k in known) or any(_inside(c, cc) and o <= oo for cc, oo in covered):
            continue
        covered.append((c, o))
        if _key(c) not in dismissed:
            chosen.append((c, o))
    return [(c, sorted(o)) for c, o in chosen], tests


def candidates(pid: str) -> list[dict]:
    """The repeated chains for a person: steps (values that differ - parameters), tests, section."""
    chosen, tests = _repeated(pid)
    try:
        doc = knowledge.get(pid)
    except Exception:
        doc = None
    out = []
    for c, ids in chosen:
        src = tests[ids[0]]
        first = _occurrences(src, c)[0]
        steps = src["steps"][first:first + len(c)]
        vals = [[s.get("value") or "" for s in tests[t]["steps"][i:i + len(c)]]
                for t in ids for i in _occurrences(tests[t], c)]
        params = {i for i, s in enumerate(steps) if s["action"] in VALUED and len({v[i] for v in vals}) > 1}
        path = start_path(src, first)
        secs = sections(pid, "\n".join(_test_text(tests[t]) for t in ids), doc) if doc else []
        out.append({"key": _key(c), "name": f"{steps[0]['description']} → {steps[-1]['description']}"[:120],
                    "section": section_label(secs, path or path_key(page_of(src, min(first + 1, len(src["steps"]))))),
                    "start_path": path, "params": len(params),
                    "steps": [{"action": s["action"], "description": s.get("description", ""),
                               "value": "‹параметр›" if i in params else (s.get("value") or "")}
                              for i, s in enumerate(steps)],
                    "tests": [{"id": t, "name": tests[t]["name"], "count": len(_occurrences(tests[t], c))}
                              for t in ids]})
    return out


def accept(pid: str, key: str, name: str = "", replace_all: bool = True) -> dict:
    """The repeated chain `key` becomes a module; with `replace_all` every occurrence in the stable tests is
    replaced with a use_module step (its values - the module's parameters) -> {"module", "replaced"}."""
    chosen, tests = _repeated(pid)
    found = next(((c, ids) for c, ids in chosen if _key(c) == key), None)
    if not found:
        raise KeyError("Цепочка шагов не найдена: тесты изменились, обновите список")
    c, ids = found
    where = [(t, i) for t in ids for i in _occurrences(tests[t], c)]
    values = {f"{t}:{i}": [s.get("value") or "" for s in tests[t]["steps"][i:i + len(c)]] for t, i in where}
    src, start = tests[where[0][0]], where[0][1]
    default = f"{src['steps'][start]['description']} → {src['steps'][start + len(c) - 1]['description']}"
    module = make_module(pid, (name or "").strip() or default, src, start, start + len(c), values, ids)
    replaced = 0
    if replace_all:
        for t, i in where:
            step_ids = [s["id"] for s in tests[t]["steps"][i:i + len(c)]]
            replaced += replace(t, step_ids, module, params_of(module, values[f"{t}:{i}"]))
    return {"module": module, "replaced": replaced}


def dismiss(pid: str, key: str) -> None:
    p = _doc_path(pid)
    with fs.lock(p):
        doc = fs.read_json(p, {}) or {}
        doc["dismissed"] = sorted(set(doc.get("dismissed") or []) | {key})
        fs.write_json(p, doc)
