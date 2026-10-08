"""The application model of a project: what the application under test is made of, so that tests
account for the dependencies that decide their result.

data/projects/<id>/knowledge.json:
    summary    what the application is, in a few lines
    entities   objects of the domain: description, which entities must exist first (`depends_on`),
               their lifecycle (states and transitions), how one is created, business rules.
               Online shop: an order depends on a product in stock and a customer with a delivery
               address; it goes new → paid → shipped → delivered.
    roles      roles of users: what each may do (`capabilities`) and may not (`restrictions`), and the
               project account (projects.accounts) that has the role
    data       test data that exists on the test stand: "product «Test product A», in stock: 10";
               `source` - who found it automatically ("" - people wrote it); `status` "needed" - data
               the scenarios need that nobody has seen on the stand yet (`needed_by` - the scenarios):
               the first test that prepares or finds it records it, and the next ones reuse it
    memory     facts learned while working: the agent's `remember` tool, people, events
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
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from pydantic import BaseModel

from . import fs, llm, projects, skills

MAX_MEMORY = 300
MAX_TEXT = 4000
LIMITS = {"entities": 200, "roles": 50, "data": 300}
FIELDS = {
    "entities": ("name", "description", "depends_on", "lifecycle", "create", "rules"),
    "roles": ("name", "description", "capabilities", "restrictions", "account"),
    "data": ("entity", "name", "details", "account", "state", "role", "source", "status", "needed_by"),
}
LISTS = ("depends_on", "needed_by")
# What a confirmation of the lifecycle covers: a change of these asks for a new one.
CONFIRMED = {"entities": ("name", "depends_on", "lifecycle", "create", "rules"),
             "roles": ("name", "capabilities", "restrictions")}


def _path(pid: str):
    return projects.path(pid) / "knowledge.json"


def _id() -> str:
    return uuid.uuid4().hex[:8]


def empty() -> dict:
    return {"summary": "", "entities": [], "roles": [], "data": [], "memory": [], "updated": None, "updated_by": "",
            "confirmed": None}


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
    c = doc.get("confirmed")
    out["confirmed"] = {k: c.get(k) for k in ("at", "by", "sig")} if isinstance(c, dict) and c.get("sig") else None
    return out


def save(pid: str, doc: dict, user: str = "") -> dict:
    """What people wrote; the confirmation stays the stored one (confirm() gives it)."""
    with fs.lock(_path(pid)):
        out = normalize(doc | {"confirmed": get(pid)["confirmed"]}) | {"updated": time.time(), "updated_by": user}
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
    return doc | {"confirmation": confirmation(doc)}


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
                         + (f" (project account «{accounts[r['account']]}»)" if r["account"] in accounts else ""))
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
    return merge(pid, found, user)


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
    return merge(pid, found)


def _data_key(d: dict) -> tuple[str, str]:
    return d["entity"].strip().lower(), d["name"].strip().lower()


def merge(pid: str, found: dict, user: str = "") -> dict:
    """Entities, roles and stand data found automatically join the model: new ones are added, known
    ones (by name; data by entity and name) get the fields they lacked and new dependencies. What
    people wrote is never overwritten; a data record found automatically (it has a `source`) is
    refreshed by a newer finding: the state of an object changes."""
    with fs.lock(_path(pid)):
        doc = get(pid)
        if found.get("summary") and not doc["summary"]:
            doc["summary"] = found["summary"]
        for kind in ("entities", "roles"):
            known = {x["name"].strip().lower(): x for x in doc[kind]}
            for item in found.get(kind) or []:
                item = _clean_item(kind, item)
                if not item:
                    continue
                old = known.get(item["name"].lower())
                if old is None:
                    doc[kind].append(item)
                    known[item["name"].lower()] = item
                    continue
                for f in FIELDS[kind]:
                    if f in LISTS:
                        old[f] = list(dict.fromkeys(old.get(f, []) + item[f]))
                    elif not old.get(f) and item.get(f):
                        old[f] = item[f]
        known = {_data_key(x): x for x in doc["data"]}
        for item in found.get("data") or []:
            item = _clean_item("data", item)
            if not item or not item["name"]:
                continue
            old = known.get(_data_key(item))
            if old is None:
                doc["data"].append(item)
                known[_data_key(item)] = item
                continue
            old["needed_by"] = list(dict.fromkeys(old["needed_by"] + item["needed_by"]))[:20]
            if item["status"] == "needed":      # one more scenario needs it: what it has is kept
                for f in ("details", "state", "role"):
                    old[f] = old[f] or item[f]
                continue
            if old["status"] == "needed":       # a test found or prepared it: it is on the stand now
                old["status"], old["source"] = "", item["source"] or "тест"
            if old["source"]:
                old["source"] = item["source"] or old["source"]
            for f in ("details", "state", "role"):
                if item[f] and (old["source"] or not old[f]):
                    old[f] = item[f]
        out = normalize(doc) | {"updated": time.time(), "updated_by": user or doc.get("updated_by", "")}
        fs.write_json(_path(pid), out, indent=1)
    return out


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
    return merge(pid, {"data": data, "roles": roles})
