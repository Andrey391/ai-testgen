"""A scripted stand-in for the model API: tests run without API calls or costs.

`FakeClient(script)` takes the place of providers.openai_compat.ChatClient: every request is recorded in `calls`
as (kind, kw) and answered by `script(kind, kw)` with a `Resp`. kind is "parse" for a request with an output
schema, else "create"; kw is the request as sent with handy views: "system" (the system prompt), "messages"
(the rest), "tools" (name, description, input_schema), "output_format" (the schema class), "timeout",
"stream".
`models()` returns `model_list` (entries of GET /models as dicts). Helpers build the usual answers: a tool call,
a text, a parsed structured output.
"""
from __future__ import annotations

import itertools
import json
import re
from types import SimpleNamespace

_ids = itertools.count(1)
STOP = {"tool_use": "tool_calls", "end_turn": "stop", "max_tokens": "length", "refusal": "stop"}


class Resp:
    def __init__(self, content=None, parsed=None, stop_reason="end_turn", model="test-model"):
        self.content = content or []
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.model = model
        self.usage = {"prompt_tokens": 1300, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 1200}}

    def response(self) -> dict:
        """The answer as a Chat Completions response."""
        texts, calls, thinking = [], [], []
        for b in self.content:
            get = b.get if isinstance(b, dict) else (lambda k, b=b: getattr(b, k, None))
            if get("type") == "thinking":
                thinking.append(get("thinking") or "")
            elif get("type") == "text":
                texts.append(get("text") or "")
            elif get("type") == "tool_use":
                calls.append({"id": get("id"), "type": "function",
                              "function": {"name": get("name"), "arguments": json.dumps(get("input") or {},
                                                                                          ensure_ascii=False)}})
        if self.parsed_output is not None:
            texts.append(self.parsed_output.model_dump_json())
        msg = {"role": "assistant", "content": "\n".join(texts) or None}
        if calls:
            msg["tool_calls"] = calls
        if thinking:
            msg["reasoning_content"] = "".join(thinking)
        if self.stop_reason == "refusal":
            msg["refusal"] = "declined"
        return {"model": self.model, "usage": self.usage,
                "choices": [{"message": msg, "finish_reason": STOP.get(self.stop_reason, "stop")}]}


def tool(name: str, **inp) -> Resp:
    return Resp([SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=inp)],
                stop_reason="tool_use")


def text(t: str) -> Resp:
    return Resp([SimpleNamespace(type="text", text=t)])


def _model_named(title: str):
    """The pydantic model a request's JSON schema was made of (its name is the class name)."""
    import pydantic

    todo = list(pydantic.BaseModel.__subclasses__())
    while todo:
        c = todo.pop()
        if c.__name__ == title and c.__module__.startswith("testgen"):
            return c
        todo.extend(c.__subclasses__())
    return None


def view(payload: dict, timeout: float = 0, stream: bool = False) -> dict:
    """The request as the scripts read it."""
    messages = list(payload["messages"])
    system = messages.pop(0)["content"] if messages and messages[0]["role"] == "system" else ""
    fmt = payload.get("response_format") or {}
    tools = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
              "input_schema": t["function"].get("parameters")} for t in payload.get("tools") or []]
    return payload | {"system": system, "messages": messages, "tools": tools, "timeout": timeout, "stream": stream,
                      "output_format": _model_named((fmt.get("json_schema") or {}).get("name", "")) if fmt else None}


class FakeClient:
    base_url = "http://fake-model"

    def __init__(self, script=None):
        self.calls: list[tuple[str, dict]] = []
        self.script = script or (lambda kind, kw: text("ok"))
        self.model_list: list[dict] = []

    async def complete(self, payload: dict, *, stream: bool = False, timeout: float = 0, on_delta=None) -> dict:
        # The session keeps appending to its message list: the request is recorded as it was sent.
        kw = view(payload, timeout, stream)
        kind = "parse" if payload.get("response_format") else "create"
        self.calls.append((kind, kw))
        resp = self.script(kind, kw)
        return resp.response() if isinstance(resp, Resp) else resp

    async def models(self) -> list[dict]:
        return list(self.model_list)


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
    """Text of the latest turn after the model's answer: the tool results and the page state with element refs."""
    out = []
    for m in reversed(kw["messages"]):
        if m["role"] == "assistant":
            break
        out[:0] = _texts(m["content"])
    return "\n".join(out)


def latest_page(kw: dict) -> str:
    """The latest page state sent to the agent (a turn may carry only an error message)."""
    for m in reversed(kw["messages"]):
        if m["role"] in ("user", "tool"):
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


def last_turn(kw: dict) -> list[dict]:
    """The messages after the model's latest answer: its tool results ("tool") and what follows them ("user")."""
    out = []
    for m in reversed(kw["messages"]):
        if m["role"] == "assistant":
            break
        out.insert(0, m)
    return out


def conversation(messages: list[dict]) -> list[str]:
    """The roles of the conversation's turns: tool results and the user message after them are one turn."""
    out: list[str] = []
    for m in messages:
        role = "assistant" if m["role"] == "assistant" else "user"
        if not out or out[-1] != role:
            out.append(role)
    return out
