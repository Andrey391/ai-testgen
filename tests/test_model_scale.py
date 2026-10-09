"""The application model of a large project and the links of its test data: the slice of the model a task
needs (with the index of the rest and the lookup), the records of a scenario followed by id to the test
that fulfils them, recipes, facts about entities, confirmation by parts, and a confirmed model that takes
new entities and roles only from a person. The model lives in tables of its own."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from helpers import arun
from testgen import analyses, fs, knowledge, pipeline, projects, storage
from testgen.agent import StudioSession
from testgen.repo.knowledge import Sql

SHOP = {"summary": "Интернет-магазин",
        "entities": [{"name": "Заказ", "group": "Заказы", "depends_on": ["Товар (в наличии)", "Покупатель"],
                      "lifecycle": "новый → оплачен (покупатель) → отправлен (менеджер)"},
                     {"name": "Товар", "group": "Каталог", "aliases": ["продукт"], "depends_on": ["Категория"],
                      "lifecycle": "черновик → в продаже"},
                     {"name": "Категория", "group": "Каталог", "lifecycle": "активна → скрыта"},
                     {"name": "Покупатель", "group": "Заказы", "lifecycle": "новый → подтверждён"},
                     {"name": "Отзыв", "group": "Отзывы", "depends_on": ["Заказ"], "lifecycle": "новый → опубликован"}],
        "roles": [{"name": "Покупатель", "capabilities": "оформляет и оплачивает заказы"},
                  {"name": "Менеджер", "capabilities": "отправляет оплаченные заказы"}]}


def _project() -> dict:
    p = projects.create("Модель " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")
    p["pipeline"]["requirements"]["confirm_model"] = True
    return projects.update(p["id"], {"pipeline": p["pipeline"]})


def _big(pid: str) -> None:
    """The shop and 400 entities of other parts of the application, 30 products, facts."""
    other = [{"name": f"Справочник {i}", "group": f"Модуль {i % 8}", "description": "запись справочника " * 4,
              "lifecycle": "черновик → активен"} for i in range(400)]
    data = [{"entity": "Товар", "name": f"Товар {i}", "state": "в наличии"} for i in range(30)]
    knowledge.save(pid, SHOP | {"entities": SHOP["entities"] + other, "data": data})
    knowledge.remember(pid, "Скидка действует только на одну категорию", about="Категория")
    for i in range(12):
        knowledge.remember(pid, f"Справочник {i} обновляется ночью")


def _fake(pid: str, name: str, scenario: str = "") -> SimpleNamespace:
    """A Studio session as far as the agent's helper tools need it."""
    say = []
    return SimpleNamespace(project_id=pid, name=name, credentials={}, data_refs={}, scenario=scenario, said=say,
                           _say=lambda who, msg: say.append(msg),
                           project={"id": pid, "pipeline": {"requirements": {"learn_model": False}}})


def test_a_large_model_gives_a_task_its_slice():
    pid = _project()["id"]
    _big(pid)
    assert not knowledge.fits(pid)
    text = knowledge.prompt(pid, knowledge.focus(text="Покупатель оплачивает заказ"))
    assert len(text) <= knowledge.PROMPT_BUDGET * knowledge.CHARS_PER_TOKEN
    # The entities the task names, everything they require first (through «Товар» to «Категория»), what requires them.
    for name in ("- Заказ", "- Товар", "- Категория", "- Покупатель"):
        assert name in text
    assert "required by: Отзыв" in text and "also called: продукт" in text
    assert "- Справочник 1:" not in text
    assert "Index of the rest of the model" in text and "Модуль 3 (50)" in text
    # The objects of an entity up to the limit, the rest counted; the facts about the slice, a tagged one too.
    assert text.count("- [Товар] Товар ") == knowledge.DATA_PER_ENTITY and "«Товар» — ещё 22" in text
    assert "Скидка действует только на одну категорию" in text and "Справочник 0 обновляется" not in text
    # Without a focus the model is cut by the budget, never past it.
    assert len(knowledge.prompt(pid)) <= knowledge.PROMPT_BUDGET * knowledge.CHARS_PER_TOKEN

    # The lookup of the agent: by another name, by a number, by a part of a word (the search of the table).
    assert "- Товар" in knowledge.lookup(pid, "продукт") and "- Категория" in knowledge.lookup(pid, "продукт")
    found = knowledge.lookup(pid, "Справочник 12")
    assert "- Справочник 12:" in found and "- Справочник 120:" not in found
    assert "Справочник" in knowledge.lookup(pid, "справ")
    assert knowledge.lookup(pid, "космодром").startswith("Nothing")
    out = arun(StudioSession._helper(_fake(pid, "Т"), "model_lookup", {"query": "Товар"}))
    assert out.count("- [Товар] Товар ") == 30


def test_the_model_lives_in_its_tables():
    pid = _project()["id"]
    fs.write_json(knowledge._path(pid), {"entities": [{"id": "e1", "name": "Заказ", "aliases": ["заявка"]}],
                                         "data": [{"id": "d1", "entity": "Заказ", "name": "Заказ 1"}],
                                         "updated": 7, "updated_by": "ann"})
    doc = knowledge.get(pid)            # a document of the old layout moves into the tables
    assert not fs.exists(knowledge._path(pid)) and doc["updated"] == 7
    assert Sql.load(pid)["entities"][0]["aliases"] == ["заявка"]
    assert [(k, x["id"]) for k, x in Sql.search(pid, ["заяв"])] == [("entities", "e1")]
    assert [(k, x["id"]) for k, x in Sql.search(pid, ["заказ"], kinds=("data",))] == [("data", "d1")]
    knowledge.save(pid, doc | {"entities": doc["entities"] + [{"id": "e2", "name": "Оплата"}]})
    assert [e["id"] for e in knowledge.get(pid)["entities"]] == ["e1", "e2"]


def test_confirmation_by_parts_of_the_model():
    p = _project()
    pid = p["id"]
    knowledge.save(pid, SHOP)
    catalog = {"title": "Товар в каталоге", "role": "Покупатель", "instructions": "Открыть товар категории"}
    order = {"title": "Оплата", "role": "Покупатель", "instructions": "Оплатить заказ"}
    assert knowledge.units_for(pid, catalog) == ["Каталог", knowledge.ROLES_UNIT]
    assert knowledge.units_for(pid, order) == ["Заказы", "Каталог", knowledge.ROLES_UNIT]     # an order needs products
    assert knowledge.units_for(pid, {"title": "Вход", "instructions": "Войти"}) is None        # names nothing: all of it

    doc = knowledge.confirm(pid, "ann", ["Каталог", knowledge.ROLES_UNIT])
    assert doc["confirmation"]["state"] == "changed"
    assert {u["unit"]: u["state"] for u in doc["confirmation"]["units"]} == {
        "Заказы": "none", "Каталог": "confirmed", "Отзывы": "none", knowledge.ROLES_UNIT: "confirmed"}
    assert knowledge.is_confirmed(pid, knowledge.units_for(pid, catalog))
    assert not knowledge.is_confirmed(pid, knowledge.units_for(pid, order)) and not knowledge.is_confirmed(pid)

    # A change of another part leaves the confirmed one as it is; a change of it asks again.
    doc = knowledge.get(pid)
    next(e for e in doc["entities"] if e["name"] == "Отзыв")["lifecycle"] += " → скрыт"
    knowledge.save(pid, doc)
    assert knowledge.is_confirmed(pid, ["Каталог", knowledge.ROLES_UNIT])
    next(e for e in doc["entities"] if e["name"] == "Категория")["rules"] = "скрытая не видна покупателю"
    knowledge.save(pid, doc)
    assert not knowledge.is_confirmed(pid, ["Каталог"])
    knowledge.confirm(pid, "bob")
    assert knowledge.is_confirmed(pid)

    # A confirmation of an earlier version (one signature of the model) counts while the model is the same.
    doc = knowledge.get(pid)
    fs.write_json(knowledge._path(pid), doc | {"confirmed": {"at": 1, "by": "old", "sig": knowledge.signature(doc)}})
    assert knowledge.is_confirmed(pid)


def test_a_confirmed_model_takes_new_entities_and_roles_from_a_person():
    p = _project()
    pid = p["id"]
    knowledge.save(pid, SHOP)
    knowledge.confirm(pid, "ann")
    doc = knowledge.need(pid, [{"title": "Приёмка", "role": "Кладовщик", "test_data": []}])
    doc = knowledge.record(pid, {"entity": "Склад", "name": "Основной склад"}, "Studio: Приёмка")
    assert "Кладовщик" not in {r["name"] for r in doc["roles"]} and "Склад" not in {e["name"] for e in doc["entities"]}
    new = {p_["kind"]: p_ for p_ in doc["pending"] if p_["match"] == "new"}
    assert new["roles"]["item"]["name"] == "Кладовщик" and new["entities"]["source"] == "Studio: Приёмка"
    assert knowledge.is_confirmed(pid)
    assert [d["entity"] for d in doc["data"]] == ["Склад"]          # the object is kept, its entity waits
    res = arun(knowledge.preflight(projects.get(pid), {"title": "Приёмка", "role": "Кладовщик"}, lifecycle=False))
    assert "ждёт подтверждения" in knowledge.preflight_text(res)
    # Found again: still one proposal; a person accepts it - the roles are to be confirmed again.
    knowledge.need(pid, [{"title": "Отгрузка", "role": "кладовщик", "test_data": []}])
    assert len([x for x in knowledge.get(pid)["pending"] if x["kind"] == "roles"]) == 1
    doc = knowledge.resolve(pid, [new["roles"]["id"]], "accept")
    assert "Кладовщик" in {r["name"] for r in doc["roles"]} and not knowledge.is_confirmed(pid)
    # What people add is theirs: added at once.
    doc = knowledge.get(pid)
    doc["entities"].append({"name": "Поставщик", "group": "Склад"})
    assert "Поставщик" in {e["name"] for e in knowledge.save(pid, doc, "ann")["entities"]}


def test_scenario_data_is_fulfilled_by_a_test_and_followed_by_id():
    p = _project()
    pid = p["id"]
    knowledge.save(pid, SHOP)
    a = analyses.create(pid, "Оплата")
    sc = analyses.add_scenario(pid, a["id"], {"title": "Оплата заказа", "role": "Покупатель", "instructions": "Оплатить",
                                              "test_data": [{"entity": "Товар", "name": "товар в наличии",
                                                             "state": "в наличии"}]})
    rid = sc["test_data"][0]["record_id"]
    rec = next(d for d in knowledge.get(pid)["data"] if d["id"] == rid)
    assert rec["status"] == "needed" and rec["needed_refs"] == [f"{a['id']}:{sc['id']}:Оплата заказа"]
    text = pipeline.scenario_text(sc)
    assert f"[id {rid}]" in text and f"[id {rid}]" in knowledge.prompt(pid)

    # The test finds the object under its own name: the needed record becomes it, the test keeps its id.
    fake = _fake(pid, "Оплата заказа", text)
    blank = {k: "" for k in ("details", "depends_on", "lifecycle", "create", "role", "login", "password")}
    out = arun(StudioSession._helper(fake, "test_data", blank | {
        "entity": "Товар", "name": "Тестовый товар А", "state": "в наличии", "fulfills": rid, "created": False}))
    assert out == "Recorded in the application model of the project."
    rec = next(d for d in knowledge.get(pid)["data"] if d["id"] == rid)
    assert rec["name"] == "Тестовый товар А" and rec["status"] == "" and rec["needed_by"] == ["Оплата заказа"]
    # A recipe: an object the test creates anew on every run.
    arun(StudioSession._helper(fake, "test_data", blank | {
        "entity": "Заказ", "name": "Заказ {{unique}}", "create": "покупатель: корзина → оформление",
        "fulfills": "", "created": True}))
    recipe = next(d for d in knowledge.get(pid)["data"] if d["entity"] == "Заказ")
    assert recipe["status"] == "recipe" and "How tests create objects" in knowledge.prompt(pid)
    assert StudioSession.refs(fake) == [{"id": rid, "use": "uses"}, {"id": recipe["id"], "use": "creates"}]
    t = storage.save({"project_id": pid, "name": "Оплата заказа", "url": "http://x", "scenario": text, "steps": [],
                      "data_refs": StudioSession.refs(fake)})
    tests = knowledge.view(pid)["data_tests"]
    assert tests[rid] == [{"id": t["id"], "name": "Оплата заказа", "use": "uses"}]
    assert tests[recipe["id"]][0]["use"] == "creates"

    # The scenario now needs other data: the model follows, the found object stays on the stand.
    sc = analyses.update_scenario(pid, a["id"], sc["id"], {"test_data": [
        {"entity": "Покупатель", "name": "покупатель с адресом", "state": ""}]})
    buyer = sc["test_data"][0]["record_id"]
    doc = knowledge.get(pid)
    assert next(d for d in doc["data"] if d["id"] == rid)["needed_refs"] == []
    assert next(d for d in doc["data"] if d["id"] == buyer)["status"] == "needed"
    # A deleted scenario: what only it needed goes.
    analyses.finish(pid, a["id"])
    analyses.delete_scenario(pid, a["id"], sc["id"])
    assert buyer not in {d["id"] for d in knowledge.get(pid)["data"]} and rid in {d["id"] for d in knowledge.get(pid)["data"]}


def test_a_needed_record_found_as_a_known_object_joins_it():
    pid = _project()["id"]
    knowledge.save(pid, SHOP | {"data": [{"entity": "Товар", "name": "Тестовый товар А", "state": "в наличии"}]})
    doc = knowledge.need(pid, [{"title": "Корзина", "test_data": [{"entity": "Товар", "name": "товар со скидкой"}]}])
    rid = next(d["id"] for d in doc["data"] if d["status"] == "needed")
    doc = knowledge.record(pid, {"entity": "Товар", "name": "Тестовый товар А"}, "Studio: Корзина", fulfills=rid)
    known = next(d for d in doc["data"] if d["name"] == "Тестовый товар А")
    assert doc["record_id"] == known["id"] and known["needed_by"] == ["Корзина"]
    assert [d["name"] for d in doc["data"]] == ["Тестовый товар А"]


def test_facts_are_about_entities_and_roles():
    pid = _project()["id"]
    knowledge.save(pid, SHOP)
    doc = knowledge.remember(pid, "Отменить можно только неоплаченный", source="Studio: t", about="Заказ, Менеджер, Склад")
    fact = doc["memory"][-1]
    order = next(e for e in doc["entities"] if e["name"] == "Заказ")
    manager = next(r for r in doc["roles"] if r["name"] == "Менеджер")
    assert fact["entities"] == [order["id"]] and fact["roles"] == [manager["id"]]
    # Duplicates merged: the facts follow the kept entity.
    doc["entities"].append({"id": "o2", "name": "Заказы", "group": "Заказы"})
    fs.write_json(knowledge._path(pid), doc)
    knowledge.merge_duplicates(pid, [{"kind": "entities", "ids": [order["id"], "o2"], "keep": "o2"}])
    assert knowledge.get(pid)["memory"][-1]["entities"] == ["o2"]
