"""Skills: instructions that shape a pipeline stage, in the SKILL.md format
(YAML-ish front matter with name / description / stage, then Markdown).

Built-in skills ship in testgen/skills/. A project can override one (same name)
or add its own in data/projects/<id>/skills/. A stage uses the skills listed in
the project's pipeline settings; their text is appended to the stage's system
prompt, after the rules they cannot override.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import fs, projects

BUILTIN = Path(__file__).resolve().parent / "skills"
STAGES = ("scenarios", "authoring", "run", "publish", "any")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
MAX_SIZE = 50_000


def parse(text: str) -> dict:
    """SKILL.md text -> {name, description, stage, body}."""
    meta, body = {}, text
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", text, re.S)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip("\"'")
    return {"name": meta.get("name", ""), "description": meta.get("description", ""),
            "stage": meta.get("stage", "any") if meta.get("stage") in STAGES else "any",
            "body": body.strip()}


def render(name: str, description: str, stage: str, body: str) -> str:
    description = " ".join(description.split())
    return f"---\nname: {name}\ndescription: {description}\nstage: {stage}\n---\n\n{body.strip()}\n"


def _project_dir(pid: str) -> Path:
    return projects.path(pid) / "skills"


def _file(pid: str | None, name: str) -> Path | None:
    if not _NAME.fullmatch(name or ""):
        return None
    if pid:
        f = _project_dir(pid) / f"{name}.md"
        if fs.is_file(f):
            return f
    f = BUILTIN / f"{name}.md"
    return f if f.exists() else None


def get(pid: str | None, name: str) -> dict | None:
    f = _file(pid, name)
    if not f:
        return None
    text = fs.read_text(f)
    s = parse(text)
    s["name"] = name
    s["text"] = text
    s["builtin"] = (BUILTIN / f"{name}.md").exists()
    s["overridden"] = s["builtin"] and f.parent != BUILTIN
    return s


def list_skills(pid: str) -> list[dict]:
    names = {f.stem for f in BUILTIN.glob("*.md")}
    names |= {f.stem for f in fs.glob(_project_dir(pid), "*.md")}
    out = [get(pid, n) for n in sorted(names) if _NAME.fullmatch(n)]
    return [{k: v for k, v in s.items() if k not in ("text", "body")} for s in out if s]


def save(pid: str, name: str, text: str) -> dict:
    """Create or override a skill. `text` is the whole SKILL.md."""
    if not _NAME.fullmatch(name or ""):
        raise ValueError("Имя скилла: латиница в нижнем регистре, цифры и дефис")
    if len(text) > MAX_SIZE:
        raise ValueError("Скилл слишком большой")
    s = parse(text)
    if not s["body"]:
        raise ValueError("Пустой скилл")
    text = render(name, s["description"], s["stage"], s["body"])
    fs.write_text(_project_dir(pid) / f"{name}.md", text)
    return get(pid, name)


def delete(pid: str, name: str) -> bool:
    """Delete a project skill; for an overridden built-in this restores the original."""
    f = _project_dir(pid) / f"{name}.md" if _NAME.fullmatch(name or "") else None
    return bool(f) and fs.unlink(f)


def prompt(pid: str, names: list[str]) -> str:
    """Text of the chosen skills for a system prompt ("" when none)."""
    parts = []
    for n in names:
        s = get(pid, n)
        if s and s["body"]:
            parts.append(f"## Skill: {n}\n{s['body']}")
    if not parts:
        return ""
    return ("\n\n# Project skills\nThe project team configured the following skills for this stage. "
            "Follow them, except where they conflict with the rules above: those rules always win.\n\n"
            + "\n\n".join(parts))
