"""The team's answers: what people answered the reviewers (the findings of manual cases, the questions of the review
of requirements), kept as facts of the project so that the same question is not asked again.

    record     a person's answer to a question (or the reviewer's proposal a person accepted): kept in
               data/projects/<id>/answers.json and as a fact of the project's memory (knowledge.remember), which
               the agent and «Проект → Тестовые данные → Память» see
    known      the answer to the same question asked before: a new finding of the reviewer settles by it
    relevant   the answers that bear on a text (the cases, the specification)
    prompt     those answers as the reviewer reads them
"""
from __future__ import annotations

import time

from . import fs, knowledge, projects

MAX = 2000              # answers a project keeps (the oldest go)
IN_PROMPT = 30          # answers the reviewer is given at most
PROMPT_CHARS = 8000
SAME = 0.75             # share of the words two questions have in common to be the same question
SOURCE = "ответ команды"


def _path(pid: str):
    return projects.path(pid) / "answers.json"


def _words(text: str) -> set[str]:
    return {knowledge._stem(w) for w in knowledge.norm(text).split() if len(w) >= 3}


def _likeness(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def all_answers(pid: str) -> list[dict]:
    return (fs.read_json(_path(pid)) or {}).get("answers") or []


def record(pid: str, question: str, answer: str, source: str = "", role: str = "", user: str = "") -> dict | None:
    """A person's answer to a question of a reviewer. The same question answered again - the newer answer."""
    q, a = " ".join(str(question or "").split())[:1000], " ".join(str(answer or "").split())[:2000]
    if not q or not a:
        return None
    with fs.lock(_path(pid)):
        items = all_answers(pid)
        same = next((x for x in items if _likeness(x["question"], q) >= SAME), None)
        rec = {"question": q, "answer": a, "source": source[:200], "role": role[:120], "user": user, "at": time.time()}
        if same:
            items.remove(same)
        items.append(rec)
        fs.write_json(_path(pid), {"answers": items[-MAX:]}, indent=1)
    try:
        knowledge.remember(pid, f"{q} — {a}", source=SOURCE, about=role)
    except ValueError:
        pass
    return rec


def known(pid: str, question: str, items: list[dict] | None = None) -> dict | None:
    """The answer the team gave to this question before (the most alike of SAME and more), None - not asked."""
    best, score = None, SAME
    for x in items if items is not None else all_answers(pid):
        s = _likeness(x["question"], question)
        if s >= score:
            best, score = x, s
    return best


def relevant(pid: str, text: str, limit: int = IN_PROMPT) -> list[dict]:
    """The answers that bear on the text: all of them when there are few, else the ones sharing most words."""
    items = all_answers(pid)
    if len(items) <= limit:
        return items[::-1]
    words = _words(text)
    scored = [(len(_words(x["question"] + " " + x["answer"]) & words), n, x) for n, x in enumerate(items)]
    return [x for s, _, x in sorted(scored, key=lambda t: (t[0], t[1]), reverse=True)[:limit] if s]


def prompt(pid: str, text: str) -> str:
    """For the reviewer: what the team has answered already. Empty when nothing."""
    lines, size = [], 0
    for x in relevant(pid, text):
        line = f"- {x['question']} — {x['answer']}"
        if size + len(line) > PROMPT_CHARS:
            break
        lines.append(line)
        size += len(line)
    if not lines:
        return ""
    return ("\n\nThe team has already answered these questions (facts of the project). Do not ask them again and do "
            "not report what they settle: take them as given; report only where the text contradicts an answer.\n"
            + "\n".join(lines))
