"""Who may do what in a project (stage 5.1).

Roles, each includes the previous one:
    viewer   reads everything of the project and runs tests and suites
    editor   writes tests (Studio, pipeline, data blocks), accepts self-healing, manages tasks
    owner    project settings, credentials, connections, members, notifications, budgets

A project is either "members" (only its members, by user or by directory group) or "open" (every
signed-in user is an editor; projects made before roles existed are open). Studio admins
(TESTGEN_ADMINS or an admin group of SSO) are owners of every project. With TESTGEN_AUTH=off
everybody is. Someone without access gets 404, as if the project did not exist.

Directory groups (SSO, LDAP) map to roles in the studio's SSO settings: {"group_roles": [{"group",
"project" ("*" = every project), "role"}], "admin_groups": [...]} (auth.sso_settings()).

The web server checks every request in one place (server.guard, by the route); the MCP server
of the studio checks its tools with the same functions.
"""
from __future__ import annotations

import contextvars

from . import auth

ROLES = ("viewer", "editor", "owner")
RANK = {r: i for i, r in enumerate(ROLES)}
LABELS = {"viewer": "наблюдатель", "editor": "редактор", "owner": "владелец"}
VISIBILITY = ("members", "open")
# The signed-in user of the current request (the server's middleware and the MCP server set it).
USER: contextvars.ContextVar[str] = contextvars.ContextVar("access_user", default="")


class Denied(Exception):
    """No access. `hidden`: the user may not even know the object exists (answer 404)."""

    def __init__(self, text: str, hidden: bool):
        super().__init__(text)
        self.hidden = hidden


def role(user: str | None, project: dict) -> str | None:
    """The user's role in the project, or None (no access)."""
    if not auth.ENABLED or auth.is_admin(user):
        return "owner"
    if not user:
        return None
    best = None
    members = project.get("members") or {}
    if members.get(user) in RANK:
        best = members[user]
    groups = set(auth.user_groups(user))
    for rule in auth.sso_settings()["group_roles"]:
        if rule.get("group") in groups and rule.get("project") in ("*", project["id"]) and rule.get("role") in RANK:
            if best is None or RANK[rule["role"]] > RANK[best]:
                best = rule["role"]
    if project.get("visibility", "open") == "open" and (best is None or RANK[best] < RANK["editor"]):
        best = "editor"
    return best


def can(user: str | None, project: dict, need: str = "viewer") -> bool:
    r = role(user, project)
    return r is not None and RANK[r] >= RANK[need]


def check(user: str | None, project: dict, need: str = "viewer", missing: str = "Проект не найден") -> str:
    """The user's role, or Denied: hidden (404, `missing`) without any access, else 403."""
    r = role(user, project)
    if r is None:
        raise Denied(missing, hidden=True)
    if RANK[r] < RANK[need]:
        raise Denied(f"Нужна роль «{LABELS[need]}» в проекте «{project.get('name', '')}» (у вас — «{LABELS[r]}»)",
                     hidden=False)
    return r


def visible(user: str | None, items: list[dict], get_project) -> list[dict]:
    """Items (with "id" of a project) the user may see."""
    out = []
    for item in items:
        p = get_project(item["id"])
        if p and can(user, p):
            out.append(item)
    return out


def normalize_members(members: dict) -> dict:
    out = {}
    for u, r in (members or {}).items():
        u = str(u).strip()
        if r not in RANK:
            raise ValueError(f"Неизвестная роль «{r}»: {', '.join(ROLES)}")
        if not auth.USERNAME.fullmatch(u):
            raise ValueError(f"Недопустимый логин участника: {u}")
        out[u] = r
    return out


def normalize_rules(rules: list[dict]) -> list[dict]:
    out = []
    for r in rules or []:
        group, project, rl = str(r.get("group", "")).strip(), str(r.get("project", "*")).strip() or "*", r.get("role")
        if not group:
            continue
        if rl not in RANK:
            raise ValueError(f"Неизвестная роль «{rl}» для группы {group}")
        out.append({"group": group, "project": project, "role": rl})
    return out
