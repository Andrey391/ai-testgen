"""Skills: instructions that shape a pipeline stage, in the SKILL.md format
(YAML-ish front matter with name / description / stage, then Markdown).

Built-in skills ship in testgen/skills/ and are read-only. A project clones one into
a local copy (same name, data/projects/<id>/skills/) and edits that; a checkbox decides
which version the project uses - the local copy or the built-in one (the names whose copy
is switched off: data/projects/<id>/skills-local.json). A project also adds skills of
its own. A stage uses the skills listed in the project's pipeline settings; their text
is appended to the stage's system prompt, after the rules they cannot override.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import fs, projects

BUILTIN = Path(__file__).resolve().parent / "skills"
STAGES = ("requirements", "scenarios", "authoring", "run", "publish", "any")
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


def _settings_path(pid: str) -> Path:
    return projects.path(pid) / "skills-local.json"


def _local_off(pid: str) -> set[str]:
    """Built-in skills whose local copy the project keeps but does not use."""
    return set((fs.read_json(_settings_path(pid)) or {}).get("off") or [])


def use_local(pid: str, name: str, on: bool) -> None:
    with fs.lock(_settings_path(pid)):
        off = _local_off(pid)
        off = off - {name} if on else off | {name}
        fs.write_json(_settings_path(pid), {"off": sorted(off)})


def _file(pid: str | None, name: str, version: str = "") -> Path | None:
    """The file of a skill: the version the project uses, or "builtin" / "local" explicitly."""
    if not _NAME.fullmatch(name or ""):
        return None
    builtin = BUILTIN / f"{name}.md"
    local = _project_dir(pid) / f"{name}.md" if pid else None
    has_local = bool(local) and fs.is_file(local)
    if version == "local":
        return local if has_local else None
    if version == "builtin":
        return builtin if builtin.exists() else None
    if has_local and (not builtin.exists() or name not in _local_off(pid)):
        return local
    return builtin if builtin.exists() else None


def get(pid: str | None, name: str, version: str = "") -> dict | None:
    f = _file(pid, name, version)
    if not f:
        return None
    text = fs.read_text(f)
    s = parse(text)
    s["name"] = name
    s["text"] = text
    s["builtin"] = (BUILTIN / f"{name}.md").exists()
    s["local"] = f.parent != BUILTIN                 # this text is the project's
    s["has_local"] = bool(pid) and fs.is_file(_project_dir(pid) / f"{name}.md")
    s["use_local"] = s["has_local"] and (not s["builtin"] or name not in _local_off(pid))
    s["overridden"] = s["builtin"] and s["use_local"]
    return s


def list_skills(pid: str) -> list[dict]:
    names = {f.stem for f in BUILTIN.glob("*.md")}
    names |= {f.stem for f in fs.glob(_project_dir(pid), "*.md")}
    out = [get(pid, n) for n in sorted(names) if _NAME.fullmatch(n)]
    return [{k: v for k, v in s.items() if k not in ("text", "body")} for s in out if s]


def clone(pid: str, name: str) -> dict:
    """A local copy of a built-in skill, used by the project from now on."""
    src = _file(None, name, "builtin")
    if not src:
        raise ValueError("Встроенный скилл не найден")
    if not fs.is_file(_project_dir(pid) / f"{name}.md"):
        fs.write_text(_project_dir(pid) / f"{name}.md", fs.read_text(src))
    use_local(pid, name, True)
    return get(pid, name)


def save(pid: str, name: str, text: str) -> dict:
    """Create a project skill or change the local copy of a built-in one (built-ins themselves are
    read-only: saving under a built-in name writes its local copy). `text` is the whole SKILL.md."""
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
    """Delete a project skill; for a built-in one its local copy goes and the original is used."""
    f = _project_dir(pid) / f"{name}.md" if _NAME.fullmatch(name or "") else None
    if f and (BUILTIN / f"{name}.md").exists():
        use_local(pid, name, True)       # a later clone is used again
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
