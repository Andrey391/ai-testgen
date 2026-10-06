"""A scripted stand-in for the LLM API: tests run without API calls or costs.

`FakeClient(script)`: every `beta.messages.create/parse` call is recorded in
`calls` and answered by `script(kind, kwargs)` with a `Resp`; `models.list()` returns
`model_list` (Models API entries as dicts). Helpers build the
usual answers: a tool call, a parsed structured output.
"""
from __future__ import annotations

import itertools
import json
import re
from types import SimpleNamespace

_ids = itertools.count(1)


class Resp:
    def __init__(self, content=None, parsed=None, stop_reason="end_turn", model="test-model"):
        self.content = content or []
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.model = model
        self.usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_creation_input_tokens=300,
                                     cache_read_input_tokens=1200)


def tool(name: str, **inp) -> Resp:
    return Resp([SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=inp)],
                stop_reason="tool_use")


def text(t: str) -> Resp:
    return Resp([SimpleNamespace(type="text", text=t)])


class FakeMessages:
    def __init__(self, owner):
        self.owner = owner

    # The session keeps appending to its message list: record the request as it was sent.
    async def create(self, **kw):
        kw = kw | {"messages": list(kw["messages"])}
        self.owner.calls.append(("create", kw))
        return self.owner.script("create", kw)

    async def parse(self, **kw):
        kw = kw | {"messages": list(kw["messages"])}
        self.owner.calls.append(("parse", kw))
        return self.owner.script("parse", kw)


class FakeClient:
    def __init__(self, script=None):
        self.calls: list[tuple[str, dict]] = []
        self.script = script or (lambda kind, kw: text("ok"))
        self.beta = SimpleNamespace(messages=FakeMessages(self))
        self.messages = self.beta.messages
        self.model_list: list[dict] = []
        self.models = SimpleNamespace(list=self._models)

    async def _models(self, **kw):
        for m in self.model_list:
            yield SimpleNamespace(to_dict=lambda m=m: m)


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    for block in content or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                out.append(block["text"])
            elif block.get("type") == "tool_result":
                out += _texts(block["content"])
        elif getattr(block, "type", "") == "text":
            out.append(block.text)
    return out


def last_user_text(kw: dict) -> str:
    """Text of the latest user turn (page state with element refs)."""
    for m in reversed(kw["messages"]):
        if m["role"] == "user":
            return "\n".join(_texts(m["content"]))
    return ""


def latest_page(kw: dict) -> str:
    """The latest page state sent to the agent (a turn may carry only an error message)."""
    for m in reversed(kw["messages"]):
        if m["role"] == "user":
            t = "\n".join(_texts(m["content"]))
            if "Elements (" in t:
                return t
    return ""


def ref_for(page_text: str, name: str) -> str:
    """The ref of an element by its accessible name in a describe() listing."""
    m = re.search(r"\[(e\d+)\] [\w-]+ \"" + re.escape(name) + r"\"", page_text)
    if not m:
        raise AssertionError(f"No element {name!r} in:\n{page_text[:2000]}")
    return m.group(1)


def dump(obj) -> str:
    """Everything that was sent to the fake API, as text (to look for secrets)."""
    return json.dumps(obj, ensure_ascii=False, default=lambda o: getattr(o, "__dict__", str(o)))
