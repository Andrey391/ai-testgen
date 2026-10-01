"""A scripted stand-in for the Claude API: tests run without API calls or costs.

`FakeClient(script)`: every `beta.messages.create/parse` call is recorded in
`calls` and answered by `script(kind, kwargs)` with a `Resp`. Helpers build the
usual answers: a tool call, a parsed structured output.
"""
from __future__ import annotations

import itertools
import json
import re
from types import SimpleNamespace

_ids = itertools.count(1)


class Resp:
    def __init__(self, content=None, parsed=None, stop_reason="end_turn", model="claude-opus-5"):
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


# ---------- other providers: fake OpenAI-compatible and GigaChat servers ----------

class FakeHttpLLM:
    """An httpx transport that plays an OpenAI-compatible server (host "openai.fake") and GigaChat
    (hosts "giga.fake" for the API and "auth.fake" for OAuth), answering with the same
    `script(kind, kw)` as FakeClient. kw["messages"] is the wire conversation flattened to
    {"role", "content": text}, so fakes.latest_page / ref_for work on it. `bodies` keeps every
    raw request (to look for secrets), `uploads` the GigaChat file uploads."""

    def __init__(self, script=None):
        import httpx
        self.script = script or (lambda kind, kw: text("ok"))
        self.calls: list[tuple[str, dict]] = []
        self.bodies: list[str] = []
        self.uploads = 0
        self.states: list[str] = []          # functions_state_id values sent back by the studio
        self.fail_first = 0                  # answer 500 to this many chat requests
        self.transport = httpx.MockTransport(self._handle)

    @staticmethod
    def _flat(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            c = m.get("content")
            if isinstance(c, list):
                c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            if m["role"] == "function":          # GigaChat: a function result is a JSON string
                try:
                    c = "\n".join(str(v) for v in json.loads(c).values())
                except (ValueError, AttributeError):
                    pass
            role = "user" if m["role"] in ("user", "tool", "function") else m["role"]
            out.append({"role": role, "content": c or ""})
        return out

    def _handle(self, request):
        import httpx
        body = request.content.decode("utf-8", "replace")
        self.bodies.append(body)
        host, path = request.url.host, request.url.path
        if host == "auth.fake":
            return httpx.Response(200, json={"access_token": "tok-1", "expires_at": 4102444800000})
        if path.endswith("/files"):
            self.uploads += 1
            return httpx.Response(200, json={"id": f"file-{self.uploads}", "object": "file"})
        data = json.loads(body)
        if self.fail_first > 0:
            self.fail_first -= 1
            return httpx.Response(500, json={"error": "overloaded"})
        kind = "parse" if data.get("response_format") or data.get("function_call") == {"name": "answer"} else "create"
        kw = {"messages": self._flat(data["messages"]), "raw": data, "system": ""}
        self.calls.append((kind, kw))
        if host == "giga.fake":
            self.states += [m["functions_state_id"] for m in data["messages"] if m.get("functions_state_id")]
        resp = self.script(kind, kw)
        return httpx.Response(200, json=self._giga(resp) if host == "giga.fake" else self._openai(resp))

    @staticmethod
    def _parts(resp):
        texts = [b.text for b in resp.content if b.type == "text"]
        call = next((b for b in resp.content if b.type == "tool_use"), None)
        if resp.parsed_output is not None:
            texts = [resp.parsed_output.model_dump_json()]
        return texts, call

    def _openai(self, resp) -> dict:
        texts, call = self._parts(resp)
        msg = {"role": "assistant", "content": "\n".join(texts) or None}
        if call:
            msg["tool_calls"] = [{"id": call.id, "type": "function",
                                  "function": {"name": call.name, "arguments": json.dumps(call.input)}}]
        return {"model": "fake-vl", "choices": [{"message": msg, "finish_reason": "tool_calls" if call else "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 50}}

    def _giga(self, resp) -> dict:
        texts, call = self._parts(resp)
        msg = {"role": "assistant", "content": "\n".join(texts)}
        if resp.parsed_output is not None:
            msg = {"role": "assistant", "content": "",
                   "function_call": {"name": "answer", "arguments": json.loads(resp.parsed_output.model_dump_json())}}
        elif call:
            msg["function_call"] = {"name": call.name, "arguments": call.input}
            msg["functions_state_id"] = f"state-{call.id}"
        return {"model": "GigaChat-2-Max", "choices": [{"message": msg, "finish_reason": "function_call"
                                                         if msg.get("function_call") else "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "precached_prompt_tokens": 200}}


def use_providers(fake: FakeHttpLLM, default: str = "anthropic", **models) -> None:
    """Register the fake servers as providers "fake-openai" and "fake-giga" (and make `default`
    the studio's default). models: extra fields for the model entries (bench...)."""
    from testgen import providers
    entry = {"input": 100, "output": 300, "currency": "RUB", "vision": True} | models
    providers.TRANSPORT = fake.transport
    providers.save_settings({"default": default, "fallbacks": [], "usd_rub": 80, "providers": [
        {"id": "fake-openai", "kind": "openai", "title": "Fake vLLM", "base_url": "http://openai.fake/v1",
         "model": "fake-vl", "vision": True, "prompt": "compact", "screenshots": "always",
         "models": [entry | {"name": "fake-vl"}]},
        {"id": "fake-giga", "kind": "gigachat", "title": "Fake GigaChat", "base_url": "http://giga.fake/api/v1",
         "auth_url": "http://auth.fake/oauth", "api_key_env": "FAKE_GIGA_KEY", "model": "GigaChat-2-Max",
         "vision": True, "prompt": "compact", "screenshots": "always",
         "models": [entry | {"name": "GigaChat-2-Max"}]},
    ]})
