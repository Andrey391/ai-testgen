"""«Справка» (guide.py): the user guide and the glossary as one page, open without signing in; the guide covers every
tab of the menu (CLAUDE.md: the guide is updated with every change people see)."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from testgen import guide
from testgen.paths import ROOT


def test_markdown_renders_headings_lists_tables_and_code():
    toc: list = []
    out = guide.render("# Т\n\n## Раз два\n\nТекст **жирно** и `код` и [ссылка](/x).\n\n- а\n  - б\n- в\n\n"
                       "1. один\n2. два\n\n| A | B |\n|---|---|\n| 1 | <2> |\n\n```\nx < y\n```\n\n> цитата", toc=toc)
    assert toc == [(1, "т", "Т"), (2, "раз-два", "Раз два")]
    assert '<h2 id="раз-два">' in out and "<b>жирно</b>" in out and "<code>код</code>" in out
    assert '<a href="/x">ссылка</a>' in out and "<ul><li>а<ul><li>б</li></ul></li><li>в</li></ul>" in out
    assert "<ol><li>один</li><li>два</li></ol>" in out and "<td>&lt;2&gt;</td>" in out
    assert "<pre><code>x &lt; y</code></pre>" in out and "<blockquote>" in out


def test_the_guide_covers_every_tab_and_opens_without_signing_in():
    import server
    r = TestClient(server.app, base_url="http://127.0.0.1:8765").get("/guide")
    assert r.status_code == 200 and "Руководство пользователя" in r.text and "Словарь терминов" in r.text
    index = (ROOT / "static" / "index.html").read_text("utf-8")
    tabs = re.findall(r'<button data-tab="\w+" data-needs-project>([^<]+)</button>', index)
    assert len(tabs) >= 7
    table = r.text.split("Где что находится", 1)[1]
    for tab in tabs:
        assert f"<b>{tab}</b>" in table, f"вкладка «{tab}» не описана в руководстве"
    assert "Импорт кейсов" in r.text and "Познакомиться с системой" in r.text
