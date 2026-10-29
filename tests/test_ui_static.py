"""Checks of the studio page source that need no browser (docs/usability-plan.md, stage 7)."""
import re
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"


def test_no_alert_confirm_prompt():
    # messages go through toast / notice / ask (with field - instead of prompt): a browser dialog blocks the page
    # and cannot name its action
    page = PAGE.read_text(encoding="utf-8")
    found = [f"{n}: {line.strip()[:120]}" for n, line in enumerate(page.splitlines(), 1)
             if re.search(r"(?<![\w.$])(window\.)?(alert|confirm|prompt)\(", line)]
    assert not found, "alert/confirm/prompt в static/index.html — используйте toast, notice или ask (field — для ввода):\n" + "\n".join(found)


def test_page_loads_nothing_from_outside():
    # the studio works without the internet (docs/offline.md): no CDN scripts, styles or fonts
    page = PAGE.read_text(encoding="utf-8")
    found = re.findall(r'<(?:script|link|img|iframe)\b[^>]*\b(?:src|href)="(?:https?:)?//[^"]+"', page)
    found += re.findall(r'@import\s+url\(["\']?(?:https?:)?//|url\(["\']?(?:https?:)?//', page)
    assert not found, "внешние ресурсы в static/index.html:\n" + "\n".join(found)


def test_screen_polls_wait_while_the_tab_is_hidden():
    # polls of the open screen go through later()/stopLater(): a hidden tab does not poll the server every second
    page = PAGE.read_text(encoding="utf-8")
    found = re.findall(r"(?:pollTimer|runTimer|suiteTimer|jobTimer|WATCH\.timer)\s*=\s*setTimeout\(", page)
    found += re.findall(r"clearTimeout\((?:pollTimer|runTimer|suiteTimer|jobTimer|WATCH\.timer)\)", page)
    assert not found, "опрос экрана мимо later()/stopLater():\n" + "\n".join(found)


def test_page_is_compressed_and_revalidated():
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    r = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip" and "<script>" in r.text
    assert r.headers["cache-control"] == "no-cache" and r.headers["etag"]
    again = client.get("/", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and not again.content
