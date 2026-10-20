"""«Справка» (GET /guide): the user guide docs/user-guide.md and the glossary docs/glossary.md as one page of the
studio, with a table of contents. A small Markdown renderer of its own (headings, paragraphs, lists, tables, code,
quotes, **bold**, *italic*, `code`, links): no dependency and nothing from the internet (TESTGEN_OFFLINE)."""
from __future__ import annotations

import html
import re

from .paths import ROOT

DOCS = ("user-guide.md", "glossary.md")

STYLE = """
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1c2230;--muted:#6b7385;--line:#e3e6ec;--accent:#3b5bdb;--soft:#eef2ff}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--panel:#1a1e27;--ink:#e6e9ef;--muted:#98a0b3;--line:#2b303c;
--accent:#6c8cff;--soft:#232a3d}}
*{box-sizing:border-box}
body{margin:0;font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink)}
.wrap{display:flex;gap:24px;max-width:1180px;margin:0 auto;padding:16px}
nav{position:sticky;top:16px;align-self:flex-start;width:260px;flex:none;max-height:calc(100vh - 32px);overflow:auto;
font-size:13px}
nav a{display:block;padding:3px 6px;border-radius:6px;color:var(--ink);text-decoration:none}
nav a:hover{background:var(--soft)}nav a.l1{font-weight:600;margin-top:8px}nav a.l3{padding-left:18px;color:var(--muted)}
main{flex:1;min-width:0;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 28px 28px}
h1{font-size:24px}h2{font-size:19px;margin-top:32px;padding-top:12px;border-top:1px solid var(--line)}h3{font-size:16px}
a{color:var(--accent)}code{background:var(--soft);padding:1px 4px;border-radius:4px;font-size:13px}
pre{background:var(--soft);padding:10px;border-radius:8px;overflow:auto}pre code{background:none;padding:0}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:14px;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{background:var(--soft)}
blockquote{margin:8px 0;padding:6px 12px;border-left:3px solid var(--accent);background:var(--soft);border-radius:0 6px 6px 0}
.top{display:flex;gap:12px;align-items:center;margin-bottom:4px}.top a{font-size:13px}
@media (max-width:820px){.wrap{display:block;padding:8px}nav{position:static;width:auto;max-height:none;margin-bottom:12px}
main{padding:4px 14px 20px}}
"""


def _slug(text: str, seen: dict) -> str:
    base = re.sub(r"[^\w\- ]", "", text.lower()).strip().replace(" ", "-") or "section"
    n = seen.get(base, 0)
    seen[base] = n + 1
    return base if not n else f"{base}-{n}"


def _inline(text: str) -> str:
    out, parts = [], re.split(r"(`[^`]+`)", text)
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
            continue
        t = html.escape(part, quote=False)
        t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: f'<a href="{html.escape(m[2])}">{m[1]}</a>', t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
        out.append(t)
    return "".join(out)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render(md: str, seen: dict | None = None, toc: list | None = None) -> str:
    """Markdown -> HTML; `toc` gets (level, id, title) of the headings up to ###."""
    seen = {} if seen is None else seen
    lines, out, i = md.replace("\r\n", "\n").split("\n"), [], 0
    para: list[str] = []

    def flush():
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("```"):
            flush()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
        elif m := re.match(r"(#{1,4})\s+(.*)", s):
            flush()
            level, title = len(m[1]), m[2].strip()
            hid = _slug(title, seen)
            if toc is not None and level <= 3:
                toc.append((level, hid, title))
            out.append(f'<h{level} id="{hid}">{_inline(title)}</h{level}>')
        elif s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            flush()
            head = _cells(s)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
                       + "</tbody></table>")
            continue
        elif re.match(r"^(\s*)([-*]|\d+\.)\s+", line):
            flush()
            out.append(_list(lines, i))
            while i < len(lines) and (re.match(r"^(\s*)([-*]|\d+\.)\s+", lines[i])
                                      or (lines[i].startswith("  ") and lines[i].strip())):
                i += 1
            continue
        elif s.startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            out.append(f"<blockquote>{render(chr(10).join(quote), seen)}</blockquote>")
            continue
        elif not s:
            flush()
        else:
            para.append(s)
        i += 1
    flush()
    return "\n".join(out)


def _list(lines: list[str], i: int) -> str:
    """A list from line i: items with their continuation lines; an indented list nests in its item."""
    first = re.match(r"^(\s*)([-*]|\d+\.)\s+", lines[i])
    indent, tag = len(first[1]), "ol" if first[2][0].isdigit() else "ul"
    items: list[list[str]] = []
    while i < len(lines):
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)", lines[i])
        if m and len(m[1]) == indent:
            items.append([m[3]])
        elif (m and len(m[1]) > indent) or (lines[i].startswith(" ") and lines[i].strip() and items):
            items[-1].append(lines[i][indent + 2:] if m else lines[i].strip())
        else:
            break
        i += 1
    body = []
    for it in items:
        text, rest = it[0], it[1:]
        nested = [r for r in rest if re.match(r"^\s*([-*]|\d+\.)\s+", r)]
        plain = [r for r in rest if r not in nested]
        inner = _inline(" ".join([text, *plain]))
        if nested:
            inner += _list(nested, 0)
        body.append(f"<li>{inner}</li>")
    return f"<{tag}>{''.join(body)}</{tag}>"


def page() -> str:
    seen: dict = {}
    toc: list = []
    parts = []
    for name in DOCS:
        path = ROOT / "docs" / name
        if path.exists():
            parts.append(render(path.read_text("utf-8"), seen, toc))
    nav = "".join(f'<a class="l{level}" href="#{hid}">{html.escape(title)}</a>' for level, hid, title in toc)
    return ("<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>Справка — AI Test Generator</title><style>{STYLE}</style></head><body><div class=\"wrap\">"
            f"<nav aria-label=\"Содержание\"><div class=\"top\"><a href=\"/\">← В студию</a></div>{nav}</nav>"
            f"<main>{''.join(parts)}</main></div></body></html>")
