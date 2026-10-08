"""The application model of a project: what the application under test is made of, so that tests
account for the dependencies that decide their result.

data/projects/<id>/knowledge.json:
    summary    what the application is, in a few lines
    entities   objects of the domain: description, which entities must exist first (`depends_on`),
               their lifecycle (states and transitions), how one is created, business rules.
               Online shop: an order depends on a product in stock and a customer with a delivery
               address; it goes new → paid → shipped → delivered.
    roles      roles of users: what each may do (`capabilities`) and may not (`restrictions`), and the
               project account (projects.accounts) that has the role (NO_LOGIN - the role works without
               logging in, like a guest)
    data       test data that exists on the test stand: "product «Test product A», in stock: 10";
               `source` - who found it automatically ("" - people wrote it); `status` "needed" - data
               the scenarios need that nobody has seen on the stand yet (`needed_by` - the scenarios):
               the first test that prepares or finds it records it, and the next ones reuse it
    memory     facts learned while working: the agent's `remember` tool, people, events
    pending    updates of existing records waiting for a person (merge()): a record found again (the same
               or a similar name - a duplicate) is not changed silently, the update is proposed and a
               person accepts or rejects each one or all of them at once (resolve())
    confirmed  who confirmed the lifecycle of the system (entities, dependencies, lifecycles, roles
               and their capabilities) and its signature: tests are generated only from a confirmed
               model (the setting "requirements.confirm_model", the pipeline waits for it); a change
               of what was confirmed asks for a new confirmation (confirmation())

It builds itself as the studio works (the setting "requirements.learn_model"): every analysis of
requirements and every exploration of the site (Planner) adds entities, roles and the data seen on
the stand (extract()), and the authoring agent records the data a test found or created and the
dependencies it discovered (the `test_data` tool -> record()), so later tests reuse them instead of
creating duplicates. People edit it in "Проект → Тестовые данные". prompt() is what
the scenarios, the authoring agent and the failure analysis are told: scenarios state their
preconditions, the agent checks that the data a step depends on exists (or prepares it), the
analysis tells a missing precondition from a product bug.

Before a test of a scenario starts (the setting "requirements.preflight") preflight() checks that the
role the scenario acts as has a project account and that the scenario does not break the restrictions
of the roles and the lifecycles of the entities (a positive scenario where a role does what it may not,
a transition the lifecycle does not allow, a precondition missing a dependency).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
import uuid

from pydantic import BaseModel

from . import fs, llm, projects, skills

MAX_MEMORY = 300
MAX_PENDING = 500
MAX_TEXT = 4000
LIMITS = {"entities": 200, "roles": 50, "data": 300}
FIELDS = {
    "entities": ("name", "description", "depends_on", "lifecycle", "create", "rules"),
    "roles": ("name", "description", "capabilities", "restrictions", "account"),
    "data": ("entity", "name", "details", "account", "state", "role", "source", "status", "needed_by"),
}
LISTS = ("depends_on", "needed_by")
# What a confirmation of the lifecycle covers: a change of these asks for a new one.
# The account of a role that works without logging in.
NO_LOGIN = "none"
GUEST = re.compile(r"гост|guest|аноним|anonym|неавториз|unauthori[sz]ed|unauthenticated|без входа|посетител|visitor")
PREFLIGHT_CACHE = 500
CONFIRMED = {"entities": ("name", "depends_on", "lifecycle", "create", "rules"),
             "roles": ("name", "capabilities", "restrictions")}


def _path(pid: str):
    return projects.path(pid) / "knowledge.json"


def _id() -> str:
    return uuid.uuid4().hex[:8]


def empty() -> dict:
    return {"summary": "", "entities": [], "roles": [], "data": [], "memory": [], "pending": [], "distinct": [],
            "updated": None, "updated_by": "", "confirmed": None}


def get(pid: str) -> dict:
    """The stored model with every field of this version (a model saved before has fewer)."""
    raw = fs.read_json(_path(pid)) or {}
    return normalize(raw) | {"updated": raw.get("updated"), "updated_by": raw.get("updated_by") or ""}


def _clean_item(kind: str, item: dict) -> dict | None:
    out = {"id": str(item.get("id") or "")[:16] or _id()}
    for f in FIELDS[kind]:
        v = item.get(f)
        if f in LISTS:
            v = v if isinstance(v, list) else str(v or "").split(",")
            out[f] = list(dict.fromkeys(str(x).strip()[:120] for x in v if str(x).strip()))[:20]
        elif f == "status":
            out[f] = "needed" if v == "needed" else ""
        else:
            out[f] = str(v or "").strip()[:MAX_TEXT]
    return out if out.get("name") or out.get("details") else None


def normalize(doc: dict) -> dict:
    out = empty()
    out["summary"] = str(doc.get("summary") or "").strip()[:MAX_TEXT]
    for kind in FIELDS:
        items = [_clean_item(kind, x) for x in doc.get(kind) or [] if isinstance(x, dict)]
        out[kind] = [x for x in items if x][:LIMITS[kind]]
    out["memory"] = [{"id": str(m.get("id") or _id())[:16], "text": str(m.get("text") or "").strip()[:1000],
                      "source": str(m.get("source") or "")[:200], "at": m.get("at") or time.time()}
                     for m in doc.get("memory") or [] if isinstance(m, dict) and str(m.get("text") or "").strip()]
    out["memory"] = out["memory"][-MAX_MEMORY:]
    out["pending"] = [p for p in (_clean_pending(x) for x in doc.get("pending") or [] if isinstance(x, dict)) if p]
    out["pending"] = out["pending"][-MAX_PENDING:]
    out["distinct"] = [str(x)[:16] for x in doc.get("distinct") or [] if x][-MAX_PENDING:]
    c = doc.get("confirmed")
    out["confirmed"] = {k: c.get(k) for k in ("at", "by", "sig")} if isinstance(c, dict) and c.get("sig") else None
    return out


def _clean_pending(p: dict) -> dict | None:
    kind = p.get("kind")
    if kind not in FIELDS or not isinstance(p.get("changes"), dict):
        return None
    item = _clean_item(kind, p.get("item") or {})
    changes = {f: v for f, v in p["changes"].items() if f in FIELDS[kind]}
    if not item or not changes:
        return None
    return {"id": str(p.get("id") or _id())[:16], "kind": kind, "target": str(p.get("target") or "")[:16],
            "match": "similar" if p.get("match") == "similar" else "same", "item": item, "changes": changes,
            "before": {f: v for f, v in (p.get("before") or {}).items() if f in changes},
            "source": str(p.get("source") or "")[:200], "at": p.get("at") or time.time()}


def save(pid: str, doc: dict, user: str = "") -> dict:
    """What people wrote; the confirmation and the pending updates stay the stored ones (confirm() and
    resolve() change them). A record people added that repeats a stored one (the same or a similar
    name) is not added twice: it becomes an update of the stored record that the person confirms."""
    with fs.lock(_path(pid)):
        stored = get(pid)
        out = normalize(doc | {k: stored[k] for k in ("confirmed", "pending", "distinct")})
        for kind in ("entities", "roles", "data"):
            known = {x["id"] for x in stored[kind]}
            fresh = [x for x in out[kind] if x["id"] not in known]
            out[kind] = [x for x in out[kind] if x["id"] in known]
            added: set = set()
            for item in fresh:
                _join(out, kind, item, f"вручную: {user}" if user else "вручную", added)
        out |= {"updated": time.time(), "updated_by": user}
        fs.write_json(_path(pid), out, indent=1)
    return view(pid)


def signature(doc: dict) -> str:
    """The lifecycle of the system as confirmed: entities, dependencies, lifecycles, roles and their capabilities."""
    part = {kind: sorted(([" ".join(str(x.get(f) or "").split()).lower() if f not in LISTS
                           else sorted(str(v).strip().lower() for v in x.get(f) or []) for f in fields]
                          for x in doc.get(kind) or []), key=str)
            for kind, fields in CONFIRMED.items()}
    return hashlib.sha256(json.dumps(part, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def missing(doc: dict) -> list[str]:
    """What the model lacks to be confirmed: the lifecycle of the system and who works with it."""
    out = []
    if not doc["entities"]:
        out.append("нет сущностей")
    elif not any(e["lifecycle"] for e in doc["entities"]):
        out.append("ни у одной сущности не описан жизненный цикл")
    if not doc["roles"]:
        out.append("нет ролей пользователей (если входа нет — роль «Гость»)")
    elif not any(r["capabilities"] for r in doc["roles"]):
        out.append("у ролей не описаны возможности")
    return out


def confirmation(doc: dict) -> dict:
    """state: none - never confirmed | changed - changed since the confirmation | confirmed."""
    c = doc.get("confirmed")
    state = "none" if not c else "confirmed" if c["sig"] == signature(doc) else "changed"
    return {"state": state, "at": (c or {}).get("at"), "by": (c or {}).get("by", ""), "missing": missing(doc)}


def view(pid: str) -> dict:
    doc = get(pid)
    return doc | {"confirmation": confirmation(doc), "duplicates": len(duplicates(doc)),
                  "role_accounts": role_accounts(pid, doc)}


def is_confirmed(pid: str) -> bool:
    return confirmation(get(pid))["state"] == "confirmed"


def confirm(pid: str, user: str = "") -> dict:
    """A person confirms the lifecycle of the system: tests may be generated from it."""
    with fs.lock(_path(pid)):
        doc = get(pid)
        lacks = missing(doc)
        if lacks:
            raise ValueError("Модель нельзя подтвердить: " + "; ".join(lacks))
        doc["confirmed"] = {"at": time.time(), "by": user, "sig": signature(doc)}
        fs.write_json(_path(pid), doc, indent=1)
    return view(pid)


def remember(pid: str, text: str, source: str = "") -> dict:
    """A fact for the project's memory (the agent's `remember` tool, an event of the studio)."""
    text = " ".join(str(text or "").split())[:1000]
    if not text:
        raise ValueError("Пустой факт")
    with fs.lock(_path(pid)):
        doc = get(pid)
        if any(m["text"] == text for m in doc["memory"]):
            return doc
        doc["memory"].append({"id": _id(), "text": text, "source": source, "at": time.time()})
        doc["memory"] = doc["memory"][-MAX_MEMORY:]
        fs.write_json(_path(pid), doc, indent=1)
    return doc


def _accounts(pid: str) -> dict[str, str]:
    return {a["id"]: a["name"] for a in projects.accounts_view(pid, full=False)}


def prompt(pid: str, limit: int = 12000) -> str:
    """The model for a prompt ("" when the project has none)."""
    doc = get(pid)
    if not any(doc[k] for k in ("summary", "entities", "roles", "data", "memory")):
        return ""
    accounts = _accounts(pid)
    lines = ["Application model of the project (entities, their dependencies and lifecycles, roles, test data that "
             "exists on the test stand). A test depends on everything it needs: the scenario names it as a "
             "precondition, the test uses the existing test data or prepares it."]
    if doc["summary"]:
        lines.append(f"About the application: {doc['summary']}")
    if doc["entities"]:
        lines.append("Entities:")
        for e in doc["entities"]:
            parts = [f"- {e['name']}" + (f": {e['description']}" if e["description"] else "")]
            if e["depends_on"]:
                parts.append(f"  requires first: {', '.join(e['depends_on'])}")
            if e["lifecycle"]:
                parts.append(f"  lifecycle: {e['lifecycle']}")
            if e["create"]:
                parts.append(f"  created by: {e['create']}")
            if e["rules"]:
                parts.append(f"  rules: {e['rules']}")
            lines += parts
    if doc["roles"]:
        lines.append("Roles:")
        for r in doc["roles"]:
            lines.append(f"- {r['name']}" + (f": {r['description']}" if r["description"] else "")
                         + (f" (project account «{accounts[r['account']]}»)" if r["account"] in accounts
                            else " (works without logging in)" if r["account"] == NO_LOGIN else ""))
            if r["capabilities"]:
                lines.append(f"  may: {r['capabilities']}")
            if r["restrictions"]:
                lines.append(f"  may not: {r['restrictions']}")

    def data_line(d: dict) -> str:
        return (f"- [{d['entity'] or 'data'}] {d['name']}" + (f" — {d['details']}" if d["details"] else "")
                + (f"; state: {d['state']}" if d["state"] else "") + (f"; role: {d['role']}" if d["role"] else "")
                + (f"; found by: {d['source']}" if d["source"] and d["status"] != "needed" else "")
                + (f"; account «{accounts[d['account']]}»" if d["account"] in accounts else "")
                + (f"; needed by: {', '.join(d['needed_by'][:5])}" if d["needed_by"] else ""))
    stand = [d for d in doc["data"] if d["status"] != "needed"]
    needed = [d for d in doc["data"] if d["status"] == "needed"]
    if stand:
        lines.append("Test data on the stand (reuse it; found by: Planner - the site map, Studio - a test that "
                     "found or created it):")
        lines += [data_line(d) for d in stand]
    if needed:
        lines.append("Test data the scenarios need that nobody has seen on the stand yet (find it or prepare it, "
                     "then record it with `test_data` so later tests reuse it):")
        lines += [data_line(d) for d in needed]
    reported = [p["item"] for p in doc["pending"] if p["kind"] == "data"]
    if reported:
        lines.append("Test data reported again by tests or analyses, the update awaits a person's confirmation "
                     "(until then the record above stays as it is):")
        lines += [data_line(d) for d in reported[-20:]]
    if doc["memory"]:
        lines.append("Remembered facts (newest last):")
        lines += [f"- {m['text']}" for m in doc["memory"][-40:]]
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n(…the model is longer)"


def account_for(pid: str, text: str) -> str:
    """The project account a scenario needs: the one of a role (or an account) its text names, else ""."""
    low = (text or "").lower()
    accounts = _accounts(pid)
    for r in get(pid)["roles"]:
        if r["account"] in accounts and r["name"] and r["name"].lower() in low:
            return r["account"]
    for aid, name in accounts.items():
        if name and name.lower() in low:
            return aid
    return ""


# ---------- before a test: the account of the role and the lifecycle ----------

def is_guest(role: dict | str) -> bool:
    """A role that works without logging in: marked so, or named like a guest."""
    if isinstance(role, dict):
        return role.get("account") == NO_LOGIN or bool(GUEST.search((role.get("name") or "").lower()))
    return bool(GUEST.search(str(role or "").lower()))


def find_role(doc: dict, name: str) -> dict | None:
    name = str(name or "").strip()
    if not name:
        return None
    return (next((r for r in doc["roles"] if norm(r["name"]) == norm(name)), None)
            or next((r for r in doc["roles"] if similar(r["name"], name)), None))


def role_accounts(pid: str, doc: dict | None = None) -> list[dict]:
    """Every role of the model with its account: ok - a test of the role can start (an account with
    a login, or the role needs no login), problem - what a person has to do first."""
    full = {a["id"]: a for a in projects.accounts_view(pid)}
    out = []
    for r in (doc or get(pid))["roles"]:
        acc = full.get(r["account"])
        guest = not acc and is_guest(r)
        problem = ("" if guest or acc and acc.get("username")
                   else f"у учётной записи «{acc['name']}» не задан логин" if acc
                   else "привязанная учётная запись удалена" if r["account"] and r["account"] != NO_LOGIN
                   else "учётная запись не заведена")
        out.append({"id": r["id"], "name": r["name"], "account": acc["id"] if acc else "",
                    "account_name": acc["name"] if acc else "", "guest": guest, "ok": not problem,
                    "problem": problem})
    return out


class XViolation(BaseModel):
    rule: str          # the restriction of a role or the lifecycle rule of the model
    problem: str       # what in the scenario breaks it
    fix: str           # how to change the scenario or the test data


class XPreflight(BaseModel):
    violations: list[XViolation]


PREFLIGHT = """You check a test scenario against the confirmed model of the application under test before a test is written for it. Report a violation only when the scenario, as written, cannot be carried out under the rules of the model:
- the role of the scenario does what its restrictions forbid (or what is not among its capabilities) AND the scenario expects it to succeed;
- the scenario expects a state transition of an entity that its lifecycle does not allow, or a transition made by a role the lifecycle does not give it to;
- the scenario works with an entity without the entities it depends on, or relies on data in a state that does not allow the action (e.g. cancels an order that is already shipped and expects success).
A negative scenario that expects the action to be denied, hidden or rejected follows the rules - it is NOT a violation. Do not report missing details, style, or rules the model does not state. Return an empty list when nothing is broken. Write `rule`, `problem` and `fix` in Russian."""


async def lifecycle_violations(project: dict, role: str, text: str) -> list[dict]:
    """What in the scenario breaks the restrictions of the roles or the lifecycles of the entities
    ([] - nothing, or the model has nothing to check against). The answer is kept for the same model
    and scenario: a retry of the test does not ask the model again."""
    pid = project["id"]
    doc = get(pid)
    if not any(e["lifecycle"] or e["depends_on"] or e["rules"] for e in doc["entities"]) \
            and not any(r["capabilities"] or r["restrictions"] for r in doc["roles"]):
        return []
    key = hashlib.sha256(json.dumps([signature(doc), role, text], ensure_ascii=False).encode()).hexdigest()[:24]
    cache_path = projects.path(pid) / "preflight.json"
    cached = (fs.read_json(cache_path) or {}).get(key)
    if isinstance(cached, list):
        return cached
    reply = await llm.parse(project["pipeline"]["scenarios"], system=PREFLIGHT + projects.language_rule(project),
                            context=prompt(pid),
                            messages=[{"role": "user", "content": (f"Role of the scenario: {role}\n" if role else "")
                                       + f"Scenario:\n{text[:20000]}"}],
                            schema=XPreflight, max_tokens=3000, project_id=pid, stage_name="preflight")
    if reply.parsed is None:
        raise RuntimeError("Модель не смогла проверить сценарий по жизненному циклу системы")
    found = [v.model_dump() for v in reply.parsed.violations if v.problem.strip()]
    with fs.lock(cache_path):
        cache = fs.read_json(cache_path) or {}
        cache[key] = found
        fs.write_json(cache_path, dict(list(cache.items())[-PREFLIGHT_CACHE:]))
    return found


async def preflight(project: dict, sc: dict) -> dict:
    """Before a test of the scenario starts. account - the account the scenario's role logs in with
    ("" - the default one), guest - the role needs no login; problems - what a person fixes first:
    an account for the role, a scenario that breaks the restrictions or the lifecycle."""
    pid = project["id"]
    doc = get(pid)
    role_name = " ".join(str(sc.get("role") or "").split())
    text = "\n".join(str(sc.get(k) or "") for k in ("title", "preconditions", "instructions",
                                                     "expected_result")).strip()
    problems, account, guest = [], "", False
    if role_name:
        role = find_role(doc, role_name)
        ra = next((x for x in role_accounts(pid, doc) if role and x["id"] == role["id"]), None)
        if ra:
            account, guest = ra["account"], ra["guest"]
            if not ra["ok"]:
                problems.append({"kind": "account", "role": ra["name"],
                                 "text": f"роль «{ra['name']}»: {ra['problem']} — заведите учётную запись в "
                                         "«Проект → Тестовые данные → Учётные записи» и привяжите её к роли"})
        elif is_guest(role_name):
            guest = True
        else:
            account = account_for(pid, role_name)
            if not account:
                problems.append({"kind": "account", "role": role_name,
                                 "text": f"роли «{role_name}» нет в модели приложения и для неё нет учётной "
                                         "записи — добавьте роль и привяжите учётную запись в «Проект → Тестовые данные»"})
    else:
        account = account_for(pid, text)
    violations = await lifecycle_violations(project, role_name, text) if text else []
    problems += [{"kind": "lifecycle", "text": f"{v['problem']} (правило: {v['rule']})"
                  + (f"; как исправить: {v['fix']}" if v.get("fix") else ""), **v} for v in violations]
    return {"ok": not problems, "account": account, "guest": guest, "role": role_name, "problems": problems}


def preflight_text(res: dict) -> str:
    return "Тест не запущен: " + "; ".join(p["text"] for p in res["problems"])


# ---------- extraction from requirements ----------

class XEntity(BaseModel):
    name: str
    description: str
    depends_on: list[str]
    lifecycle: str
    create: str
    rules: str


class XRole(BaseModel):
    name: str
    description: str
    capabilities: str = ""
    restrictions: str = ""


class XData(BaseModel):
    entity: str
    name: str
    details: str
    state: str


class XModel(BaseModel):
    summary: str
    entities: list[XEntity]
    roles: list[XRole]
    data: list[XData] = []


EXTRACT = """You are a business analyst. From the requirements, build the model of the application under test that a test engineer needs to prepare correct test data: the domain entities, for each one which other entities (or settings of them) must exist before it can be created (`depends_on`, by entity names), its lifecycle (states and allowed transitions, e.g. "new → paid → shipped → delivered; a shipped order cannot be cancelled"), how and by which role it is created, and its business rules; the roles of users with their capabilities (what each role may do: which entities it sees, creates, changes, moves to which state) and restrictions (what it may not do). Write a lifecycle with the role that makes each transition: "new → paid (customer) → shipped (shop manager) → delivered; a shipped order cannot be cancelled". Example: in an online shop "Order" depends on "Product" (in stock) and "Customer" (with a delivery address), the customer creates it at checkout; "Product" depends on "Category", the shop manager creates it; the role "Customer" may: browse the catalog, create and pay own orders, cancel an unpaid order; may not: see other customers' orders, change prices. When the application has no login, the role is "Guest".

Take only what the text says or clearly implies; leave a field empty when unknown. Merge with the model you are given: keep its entities and roles (by name) and add what is new. Write in the language of the text."""

EXPLORE = """The text is not a specification but the map of the site made by an automatic walk through its pages (titles, forms, buttons, links, text). Infer the entities, their dependencies and lifecycles and the roles from what the pages show (a cart and a checkout form mean "Order" depends on "Product" and "Cart"). Also list in `data` the concrete objects that already exist on the test stand and that tests can rely on: e.g. the product «Test product A» of the category «Category 1», in stock, price 1000 - its entity, its name as shown, details and state. Only objects the pages actually show, at most 50."""

SOURCES = {"requirements": ("Requirements", "", "требования"), "explore": ("Map of the site", EXPLORE, "Planner")}


async def extract(project: dict, requirements: str, user: str = "", source: str = "requirements") -> dict:
    """Add what the requirements (or a map of the site, `source="explore"`) say about entities,
    dependencies, lifecycles, roles and the data on the stand to the model."""
    pid = project["id"]
    cfg = project["pipeline"]["requirements"]
    title, extra, label = SOURCES[source]
    current = prompt(pid)
    system = (EXTRACT + (f"\n\n{extra}" if extra else "") + projects.language_rule(project)
              + skills.prompt(pid, cfg.get("model_skills") or []))
    reply = await llm.parse(cfg, system=system,
                            context=f"{title}:\n{requirements[:150_000]}",
                            messages=[{"role": "user", "content": (f"The current model:\n{current}\n\n" if current
                                                                   else "") + "Build the application model."}],
                            schema=XModel, max_tokens=12000, project_id=pid, stage_name="requirements")
    if reply.parsed is None:
        raise RuntimeError("Модель не смогла выделить сущности из требований")
    found = reply.parsed.model_dump()
    found["data"] = [d | {"source": label} for d in found.get("data") or []]
    return merge(pid, found, user, source=label)


def record(pid: str, item: dict, source: str) -> dict:
    """Test data a test found or created (the agent's `test_data` tool): the record joins the stand
    data (by entity and name), its entity - the entities, with the dependencies and lifecycle seen."""
    entity = " ".join(str(item.get("entity") or "").split())
    name = " ".join(str(item.get("name") or "").split())
    if not entity or not name:
        raise ValueError("Укажите сущность и объект")
    depends = item.get("depends_on") or []
    found = {"data": [{"entity": entity, "name": name, "details": item.get("details") or "",
                       "state": item.get("state") or "", "role": item.get("role") or "", "source": source}],
             "entities": [{"name": entity, "depends_on": depends if isinstance(depends, list) else str(depends).split(","),
                           "lifecycle": item.get("lifecycle") or "", "create": item.get("create") or ""}]}
    return merge(pid, found, source=source)


def norm(text: str) -> str:
    """A name to compare: case, «ё», quotes, punctuation and spaces do not matter."""
    text = str(text or "").lower().replace("ё", "е")
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def similar(a: str, b: str, loose: bool = False) -> bool:
    """The same name written differently: «Заказ» - «заказы», «Тестовый товар А» - «тестовый товар "А"».
    Numbers must be the same: «Заказ 1» and «Заказ 2» are different objects. `loose` (the duplicate
    check people run) also takes a name contained in another one: «Менеджер» - «Менеджер магазина»."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if re.findall(r"\d+", na) != re.findall(r"\d+", nb):
        return False
    wa, wb = set(na.split()), set(nb.split())
    if wa == wb or loose and min(len(na), len(nb)) >= 4 and (wa <= wb or wb <= wa):
        return True
    m = difflib.SequenceMatcher(None, na, nb)
    return min(len(na), len(nb)) >= 4 and m.quick_ratio() >= 0.85 and m.ratio() >= 0.85


def _same(kind: str, a: dict, b: dict, loose: bool = False) -> bool:
    if kind == "data":
        return similar(a["entity"], b["entity"]) and similar(a["name"], b["name"], loose)
    return similar(a["name"], b["name"], loose)


def _key(kind: str, x: dict) -> tuple:
    return (norm(x["entity"]), norm(x["name"])) if kind == "data" else (norm(x["name"]),)


def _find(doc: dict, kind: str, item: dict) -> tuple[dict | None, str]:
    """The stored record an incoming one repeats: the same name first, then a similar one."""
    key = _key(kind, item)
    for x in doc[kind]:
        if _key(kind, x) == key:
            return x, "same"
    for x in doc[kind]:
        if _same(kind, x, item):
            return x, "similar"
    return None, ""


def _changes(kind: str, old: dict, new: dict) -> dict:
    """What an incoming record would change in a stored one: new dependencies, filled or different
    fields, the data found on the stand. Nothing when it says nothing new."""
    out = {}
    for f in FIELDS[kind]:
        if f in ("name", "entity", "account", "source", "status", "needed_by"):
            continue
        if f in LISTS:
            have = {norm(v) for v in old[f]}
            add = [v for v in new[f] if norm(v) not in have]
            if add:
                out[f] = old[f] + add
        elif new[f] and norm(new[f]) not in norm(old[f]):
            out[f] = new[f]
    if kind == "data" and old["status"] == "needed" and new["status"] != "needed":
        out["status"] = ""                  # a test found or prepared it: it is on the stand now
    if kind == "data" and out and new["source"] and (old["source"] or old["status"] == "needed"):
        out["source"] = new["source"]       # who found it last; a record people wrote stays theirs
    return out


def _apply(item: dict, changes: dict) -> None:
    for f, v in changes.items():
        item[f] = list(dict.fromkeys(item[f] + list(v or [])))[:20] if f in LISTS else v


def _join(doc: dict, kind: str, item: dict, source: str, added: set) -> None:
    """An incoming record joins the model: a new one is added; one that repeats a stored record (the
    same or a similar name) proposes an update of it, and a person confirms it (resolve()). Which
    scenarios need a data record is noted at once: it changes nothing in the record itself."""
    old, match = _find(doc, kind, item)
    if old is None:
        doc[kind].append(item)
        added.add(item["id"])
        return
    if kind == "data":
        old["needed_by"] = list(dict.fromkeys(old["needed_by"] + item["needed_by"]))[:20]
        if item["status"] == "needed":      # one more scenario needs it: what the record says is kept
            if old["id"] in added:
                _apply(old, {f: item[f] for f in ("details", "state", "role") if item[f] and not old[f]})
            return
    changes = _changes(kind, old, item)
    if not changes:
        return
    if old["id"] in added:                  # repeated within one finding: nobody has seen it yet
        _apply(old, changes)
        return
    prev = next((p for p in doc["pending"] if p["kind"] == kind and p["target"] == old["id"]), None)
    if prev:                                # one proposal per record: the newer finding joins it
        doc["pending"].remove(prev)
        joined = prev["changes"] | changes
        for f in LISTS:
            if f in prev["changes"] and f in changes:
                joined[f] = list(dict.fromkeys(prev["changes"][f] + changes[f]))
        changes = joined
        match = "same" if prev["match"] == match == "same" else "similar"
    doc["pending"].append({"id": _id(), "kind": kind, "target": old["id"], "match": match, "item": item,
                           "changes": changes, "before": {f: old[f] for f in changes},
                           "source": source or item.get("source") or "", "at": time.time()})
    doc["pending"] = doc["pending"][-MAX_PENDING:]


def merge(pid: str, found: dict, user: str = "", source: str = "") -> dict:
    """Entities, roles and stand data found automatically join the model: new ones are added; a record
    that is already there (by name, data by entity and name; the same or a similar one - a duplicate)
    is not changed silently: its update waits in `pending` for a person (resolve()). `source` - who found
    them (requirements, Planner, a test in Studio)."""
    with fs.lock(_path(pid)):
        doc = get(pid)
        if found.get("summary") and not doc["summary"]:
            doc["summary"] = found["summary"]
        added: set = set()
        for kind in ("entities", "roles", "data"):
            for item in found.get(kind) or []:
                item = _clean_item(kind, item)
                if item and item["name"]:
                    _join(doc, kind, item, item.get("source") or source or user, added)
        out = normalize(doc) | {"updated": time.time(), "updated_by": user or doc.get("updated_by", "")}
        fs.write_json(_path(pid), out, indent=1)
    return out


def resolve(pid: str, ids: list[str] | None, action: str, user: str = "") -> dict:
    """A person decides on pending updates (`ids`; None - all of them): accept - the stored record is
    updated, reject - the update is dropped, separate - the incoming record is added as a new one (it
    only looked like the stored one)."""
    if action not in ("accept", "reject", "separate"):
        raise ValueError("Неизвестное действие")
    with fs.lock(_path(pid)):
        doc = get(pid)
        for p in [p for p in doc["pending"] if ids is None or p["id"] in ids]:
            doc["pending"].remove(p)
            if action == "reject":
                continue
            target = next((x for x in doc[p["kind"]] if x["id"] == p["target"]), None)
            if action == "separate" or target is None:      # the record was deleted meanwhile: it comes back
                doc[p["kind"]].append(p["item"] | {"id": _id()})
            else:
                _apply(target, p["changes"])
        out = normalize(doc) | {"updated": time.time(), "updated_by": user or doc.get("updated_by", "")}
        fs.write_json(_path(pid), out, indent=1)
    return view(pid)


# ---------- duplicates people check ----------

def duplicates(doc: dict) -> list[dict]:
    """Groups of records of one kind that name the same thing (similar names, loosely), the record to
    keep - the one people wrote, else the fullest - and what the merged record would be."""
    groups = []
    for kind in ("entities", "roles", "data"):
        items = doc[kind]
        parent = list(range(len(items)))

        def root(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if root(i) != root(j) and _same(kind, items[i], items[j], loose=True):
                    parent[root(j)] = root(i)
        by_root: dict[int, list[dict]] = {}
        for i, x in enumerate(items):
            by_root.setdefault(root(i), []).append(x)
        for members in (m for m in by_root.values() if len(m) > 1):
            gid = group_id(members)
            if gid in doc.get("distinct", []):
                continue                    # a person said these are different things
            keep = max(members, key=lambda x: (not x.get("source"), x.get("status") != "needed",
                                               sum(1 for v in x.values() if v)))
            groups.append({"id": gid, "kind": kind, "keep": keep["id"], "items": members,
                           "merged": _merged(kind, keep, members)})
    return groups


def group_id(members: list[dict]) -> str:
    return hashlib.sha1("|".join(sorted(x["id"] for x in members)).encode()).hexdigest()[:12]


def dismiss_duplicates(pid: str, group_ids: list[str], user: str = "") -> dict:
    """A person says the records of these groups are different things: the check no longer shows them
    (until the group changes)."""
    with fs.lock(_path(pid)):
        doc = get(pid)
        doc["distinct"] = list(dict.fromkeys(doc["distinct"] + [str(g)[:16] for g in group_ids if g]))
        out = normalize(doc) | {"updated": time.time(), "updated_by": user or doc.get("updated_by", "")}
        fs.write_json(_path(pid), out, indent=1)
    return view(pid)


def _merged(kind: str, keep: dict, members: list[dict]) -> dict:
    out = json.loads(json.dumps(keep))
    for x in members:
        if x["id"] == keep["id"]:
            continue
        for f in FIELDS[kind]:
            if f in LISTS:
                out[f] = list(dict.fromkeys(out[f] + x[f]))[:20]
            elif f == "status":
                out[f] = "" if "" in (out[f], x[f]) else out[f]
            elif not out[f] and x[f]:
                out[f] = x[f]
    return out


def merge_duplicates(pid: str, groups: list[dict] | None, user: str = "") -> dict:
    """Merges groups of duplicates ({kind, ids, keep}; None - every group found): the kept record gets
    what the others had, the others are removed, references to their names (dependencies, the entity
    and the role of data) point to the kept one, their pending updates - to it too."""
    with fs.lock(_path(pid)):
        doc = get(pid)
        if groups is None:
            groups = [{"kind": g["kind"], "ids": [x["id"] for x in g["items"]], "keep": g["keep"]}
                      for g in duplicates(doc)]
        merged = 0
        for g in groups:
            kind = g.get("kind")
            if kind not in ("entities", "roles", "data"):
                continue
            ids = set(g.get("ids") or [])
            members = [x for x in doc[kind] if x["id"] in ids]
            keep = next((x for x in members if x["id"] == g.get("keep")), members[0] if members else None)
            if keep is None or len(members) < 2:
                continue
            result = _merged(kind, keep, members)
            gone = {x["id"] for x in members} - {keep["id"]}
            names = {norm(x["name"]) for x in members if x["id"] in gone} - {norm(result["name"])}
            doc[kind] = [result if x["id"] == keep["id"] else x for x in doc[kind] if x["id"] not in gone]
            for p in doc["pending"]:
                if p["kind"] == kind and p["target"] in gone:
                    p["target"] = keep["id"]
            if kind == "entities":
                for e in doc["entities"]:
                    e["depends_on"] = list(dict.fromkeys(result["name"] if norm(v) in names else v
                                                         for v in e["depends_on"]))
            for d in doc["data"]:
                if kind == "entities" and norm(d["entity"]) in names:
                    d["entity"] = result["name"]
                if kind == "roles" and norm(d["role"]) in names:
                    d["role"] = result["name"]
            merged += 1
        out = normalize(doc) | {"updated": time.time(), "updated_by": user or doc.get("updated_by", "")}
        fs.write_json(_path(pid), out, indent=1)
    return view(pid) | {"merged": merged}


def need(pid: str, scenarios: list[dict]) -> dict:
    """The test data the scenarios need (`test_data` of a scenario) joins the model: data on the stand
    gets the scenarios that rely on it, unknown data is added as "needed" - the first test that finds or
    prepares it records it (record()), the next ones reuse it. Roles the scenarios act as join the roles."""
    data, roles = [], []
    for sc in scenarios:
        title = str(sc.get("title") or "").strip()
        for d in sc.get("test_data") or []:
            if isinstance(d, dict) and d.get("entity") and d.get("name"):
                data.append({k: d.get(k) or "" for k in ("entity", "name", "details", "state", "role")}
                            | {"status": "needed", "needed_by": [title] if title else []})
        if str(sc.get("role") or "").strip():
            roles.append({"name": sc["role"]})
    return merge(pid, {"data": data, "roles": roles}, source="сценарии")
