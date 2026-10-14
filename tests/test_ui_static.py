"""Checks of the studio page source that need no browser (docs/usability-plan.md, stage 7)."""
import re
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"


def test_no_alert_and_confirm():
    # messages go through toast / notice / ask: a browser alert blocks the page and cannot name its action
    page = PAGE.read_text(encoding="utf-8")
    found = [f"{n}: {line.strip()[:120]}" for n, line in enumerate(page.splitlines(), 1)
             if re.search(r"(?<![\w.$])(window\.)?(alert|confirm)\(", line)]
    assert not found, "alert/confirm в static/index.html — используйте toast, notice или ask:\n" + "\n".join(found)
