"""The stand of a run: navigate steps recorded on the test's site go to the project's address (steps.rebase)."""
from __future__ import annotations

from helpers import arun
from testgen import steps
from testgen.steps import new_step, rebase

OLD = ["https://old.shop.example"]


def test_recorded_origin_goes_to_the_stand():
    assert rebase("https://old.shop.example/cart?id=1#top", OLD, "https://stage.shop.example") == \
        "https://stage.shop.example/cart?id=1#top"
    # only the origin of the stand: its path is where the explorer starts, not a prefix of every address
    assert rebase("https://old.shop.example/login", OLD, "http://127.0.0.1:8000/app/") == "http://127.0.0.1:8000/login"
    assert rebase("https://OLD.shop.example/a", OLD, "https://stage.shop.example") == "https://stage.shop.example/a"


def test_other_sites_and_no_stand_stay_as_recorded():
    assert rebase("https://pay.example.com/checkout", OLD, "https://stage.shop.example") == \
        "https://pay.example.com/checkout"
    assert rebase("https://old.shop.example:8443/a", OLD, "https://stage.shop.example") == \
        "https://old.shop.example:8443/a"                  # another port is another site
    assert rebase("https://old.shop.example/a", OLD, "") == "https://old.shop.example/a"
    assert rebase("https://old.shop.example/a", [], "https://stage.shop.example") == "https://old.shop.example/a"
    assert rebase("/relative", OLD, "https://stage.shop.example") == "/relative"


class _Page:
    def __init__(self):
        self.visited = []

    async def goto(self, url, **kw):
        self.visited.append(url)


class _Session:
    """Just what perform() needs for a navigate step."""
    dialog_error = ""

    def __init__(self, options):
        self.page, self.options = _Page(), options

    def settled_quietly(self, value=True):
        pass

    def expand(self, value):
        return value

    async def settle(self):
        pass


def test_navigate_goes_to_the_stand_of_the_run():
    bs = _Session({"stand_url": "https://stage.shop.example", "recorded_origins": list(OLD)})
    arun(steps.perform(bs, new_step("navigate", "Корзина", "https://old.shop.example/cart")))
    arun(steps.perform(bs, new_step("navigate", "Оплата", "https://pay.example.com/checkout")))
    assert bs.page.visited == ["https://stage.shop.example/cart", "https://pay.example.com/checkout"]
    plain = _Session({})                     # a session without a stand (a new Studio session) goes where it is told
    arun(steps.perform(plain, new_step("navigate", "Корзина", "https://old.shop.example/cart")))
    assert plain.page.visited == ["https://old.shop.example/cart"]


def test_cli_rejects_a_stand_without_scheme(monkeypatch, capsys):
    from testgen import db, run
    monkeypatch.setattr(db, "check", lambda: None)
    monkeypatch.setattr(run, "find_project", lambda ref: {"id": "p1", "name": "Shop", "base_url": "https://old"})
    assert run.main(["--project", "Shop", "--base-url", "stage.shop.example"]) == 2
    assert "--base-url" in capsys.readouterr().err
