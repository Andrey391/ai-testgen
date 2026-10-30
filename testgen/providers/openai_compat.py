"""Requests to the model through an OpenAI-compatible Chat Completions API: vLLM, Ollama, LiteLLM, YandexGPT,
GigaChat behind a compatible gateway - any server with POST {base}/chat/completions and GET {base}/models.

The studio speaks its neutral format (base.py); here it becomes Chat Completions and back:

    tools            [{"type": "function", "function": {name, description, parameters}}]
    tool_use         an assistant message with tool_calls (arguments as JSON text)
    tool_result      a "tool" message; its screenshots go in a user message right after the tool messages
    image            {"type": "image_url", "image_url": {"url": "data:<type>;base64,..."}}
    structured       response_format json_schema; off - the schema in the prompt (llm.parse)
    effort           reasoning_effort (low / medium / high)

A gateway that refuses an optional part of a request ("... is not supported") gets the request again without it
(OPTIONAL, REFUSED); structured output refused - FeatureRefused, the schema goes into the prompt.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from .base import ProviderError, Reply, Request, fill_empty, trim_history, usage_dict

FEATURES = ("effort", "structured")
STOP = {"tool_calls": "tool", "function_call": "tool", "stop": "end", "eos": "end", "length": "max_tokens",
        "content_filter": "refusal"}
EFFORT = {"low": "low", "medium": "medium", "high": "high"}
CONNECT_TIMEOUT = 30.0
DEFAULT_TIMEOUT = 600.0

NO_KEY = "API ИИ: неверный или не заданный API-ключ (укажите его в «Проект → Модель»)."
NO_CREDITS = ("API ИИ: у аккаунта, к которому относится API-ключ проекта, закончились средства или квота. "
              "Пополните баланс у поставщика модели или укажите другой ключ в «Проект → Модель».")
NO_ADDRESS = "Не задан адрес API модели: укажите его в «Проект → Модель»."


class ApiError(Exception):
    """An HTTP error of the model API: status, its message and the parsed body."""

    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(message)
        self.status_code, self.message, self.body = status, message, body


def _message_of(body: Any, fallback: str) -> str:
    err = body.get("error", body) if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("detail") or fallback)
    if isinstance(err, str):
        return err
    return fallback


def _no_credits(e: ApiError) -> bool:
    text = f"{e.message} {e.body}".lower()
    return e.status_code == 402 or any(k in text for k in ("insufficient_quota", "insufficient balance",
                                                           "credit balance", "quota exceeded"))


def error_text(e: Exception) -> str:
    if isinstance(e, ApiError):
        if e.status_code in (401, 403):
            return NO_KEY
        if _no_credits(e):
            return NO_CREDITS
        if e.status_code == 429:
            return "API ИИ: превышен лимит запросов, повторите чуть позже."
        return f"Ошибка API ИИ {e.status_code}: {e.message}"
    if isinstance(e, httpx.TimeoutException):
        return "API ИИ: модель не ответила за отведённое время — повторите или выберите модель быстрее."
    if isinstance(e, httpx.TransportError):
        return "API ИИ: ошибка сети."
    if isinstance(e, (RuntimeError, ValueError)) and str(e):      # the studio's own words for a person
        return str(e)
    return f"{type(e).__name__}: {e}"


def is_error(e: Exception) -> bool:
    return isinstance(e, (ApiError, httpx.TransportError))


# ---------- the HTTP client ----------

class ChatClient:
    """POST {base_url}/chat/completions and GET {base_url}/models with the key as "Authorization: Bearer".
    complete() returns the answer as one Chat Completions response, a streamed one assembled from its chunks."""

    def __init__(self, api_key: str, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(headers=headers, transport=transport,
                                       timeout=httpx.Timeout(DEFAULT_TIMEOUT, connect=CONNECT_TIMEOUT))

    @staticmethod
    def _timeout(seconds: float) -> httpx.Timeout | None:
        """The request's own wait: for a stream - the longest pause in it, so a long answer still arriving is not cut."""
        return httpx.Timeout(seconds, connect=min(seconds, CONNECT_TIMEOUT)) if seconds else None

    @staticmethod
    async def _raise(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        raw = (await resp.aread()).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw
        raise ApiError(resp.status_code, _message_of(body, raw[:500] or resp.reason_phrase), body)

    async def models(self) -> list[dict]:
        resp = await self._http.get(f"{self.base_url}/models")
        await self._raise(resp)
        data = resp.json()
        return list(data.get("data") or []) if isinstance(data, dict) else list(data or [])

    async def complete(self, payload: dict, *, stream: bool = False, timeout: float = 0, on_delta=None) -> dict:
        """on_delta(thinking chars, answer chars) after every chunk of a stream; True - give the answer up there,
        what came is returned as cut off (finish_reason "length")."""
        kw = {"timeout": self._timeout(timeout)} if timeout else {}
        url = f"{self.base_url}/chat/completions"
        if not stream:
            resp = await self._http.post(url, json=payload, **kw)
            await self._raise(resp)
            return resp.json()
        body = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        acc = _Assembly()
        async with self._http.stream("POST", url, json=body, **kw) as resp:
            await self._raise(resp)
            try:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    acc.add(json.loads(data))
                    if on_delta and on_delta(acc.thought, acc.said):
                        return acc.result(cut=True)
            except httpx.ReadTimeout:
                if acc.thought and not acc.said:        # went quiet while still thinking: as cut off there
                    return acc.result(cut=True)
                raise
        return acc.result()


class _Assembly:
    """A streamed answer put together from its chunks."""

    def __init__(self):
        self.model, self.content, self.reasoning, self.refusal = "", [], [], []
        self.calls: dict[int, dict] = {}
        self.finish, self.usage = None, None

    @property
    def thought(self) -> int:
        return sum(map(len, self.reasoning))

    @property
    def said(self) -> int:
        return sum(map(len, self.content)) + sum(len(c["function"]["arguments"]) for c in self.calls.values())

    def add(self, chunk: dict) -> None:
        self.model = chunk.get("model") or self.model
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            d = choice.get("delta") or {}
            if d.get("content"):
                self.content.append(d["content"])
            if d.get("reasoning_content") or d.get("reasoning"):
                self.reasoning.append(d.get("reasoning_content") or d.get("reasoning"))
            if d.get("refusal"):
                self.refusal.append(d["refusal"])
            for tc in d.get("tool_calls") or []:
                c = self.calls.setdefault(tc.get("index", len(self.calls)),
                                          {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                c["id"] = tc.get("id") or c["id"]
                fn = tc.get("function") or {}
                c["function"]["name"] += fn.get("name") or ""
                c["function"]["arguments"] += fn.get("arguments") or ""
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]

    def result(self, cut: bool = False) -> dict:
        msg = {"role": "assistant", "content": "".join(self.content) or None}
        if self.reasoning:
            msg["reasoning_content"] = "".join(self.reasoning)
        if self.refusal:
            msg["refusal"] = "".join(self.refusal)
        if self.calls:
            msg["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        return {"model": self.model, "usage": self.usage,
                "choices": [{"message": msg, "finish_reason": "length" if cut else (self.finish or "stop")}]}


# ---------- the neutral format <-> Chat Completions ----------

def _image(block: dict) -> dict:
    src = block.get("source") or {}
    url = src.get("url") or f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
    return {"type": "image_url", "image_url": {"url": url}}


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def _parts(blocks: list[dict]) -> list[dict]:
    out = []
    for b in blocks:
        if b.get("type") == "text" and b.get("text"):
            out.append({"type": "text", "text": b["text"]})
        elif b.get("type") == "image":
            out.append(_image(b))
    return out


def tools_of(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("input_schema") or {"type": "object"}}}
            for t in tools]


def messages_of(system: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}] if system else []
    for m in messages:
        content = m["content"]
        if m["role"] == "assistant":
            if isinstance(content, str):
                out.append({"role": "assistant", "content": content})
                continue
            text = "\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text"))
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                     for b in content if b.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        if isinstance(content, str):
            out.append({"role": "user", "content": content})
            continue
        # A user turn: the tool results first (they must follow the assistant's calls), then everything else -
        # the screenshots of the results too: a tool message carries text only.
        rest: list[dict] = []
        for b in content:
            if b.get("type") == "tool_result":
                inner = b.get("content")
                blocks = [{"type": "text", "text": inner}] if isinstance(inner, str) else list(inner or [])
                text = _text_of(blocks) or "(no output)"
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                            "content": ("Error: " + text) if b.get("is_error") else text})
                images = [x for x in blocks if x.get("type") == "image"]
                if images:
                    rest += [{"type": "text", "text": "Screenshot after the tool call above:"}, *images]
            else:
                rest.append(b)
        parts = _parts(rest)
        if parts:
            out.append({"role": "user", "content": parts[0]["text"] if len(parts) == 1 and parts[0]["type"] == "text"
                        else parts})
    return out


def _input(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    try:
        value = json.loads(arguments or "{}")
    except ValueError:
        return {"__raw__": arguments}       # check_call sends it back to the model
    return value if isinstance(value, dict) else {"__raw__": arguments}


def reply_of(resp: dict) -> Reply:
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict] = []
    thinking = msg.get("reasoning_content") or msg.get("reasoning")
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"] if isinstance(msg["content"], str)
                        else _text_of(msg["content"])})
    for i, c in enumerate(msg.get("tool_calls") or []):
        fn = c.get("function") or {}
        content.append({"type": "tool_use", "id": c.get("id") or f"call_{i}", "name": fn.get("name", ""),
                        "input": _input(fn.get("arguments"))})
    stop = "refusal" if msg.get("refusal") else STOP.get(choice.get("finish_reason") or "", "end")
    if stop == "end" and any(b["type"] == "tool_use" for b in content):
        stop = "tool"
    u = resp.get("usage") or {}
    cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    return Reply(content=content, stop=stop, model=resp.get("model") or "",
                 usage=usage_dict(max(int(u.get("prompt_tokens") or 0) - cached, 0), u.get("completion_tokens") or 0,
                                  0, cached))


# A gateway may refuse a part of a request the studio can do without: "tool_choice is not supported",
# "response_format json_schema is not supported", "Extra inputs are not permitted". The part is dropped and the
# request sent again, whatever the part. What names each optional part in such an error:
OPTIONAL = {
    "structured": ("json_schema", "response_format", "structured output"),
    "tool_choice": ("tool_choice",),
    "parallel": ("parallel_tool_calls",),
    "effort": ("reasoning_effort", "reasoning", "effort"),
}
# The order a refusal that names none of them drops them: the least needed first.
DROP_ORDER = ("effort", "parallel", "tool_choice", "structured")
UNSUPPORTED = re.compile(r"not (?:be )?supported|unsupported|feature_not_supported|not allowed|not permitted|"
                         r"unknown (?:parameter|field|argument)|unrecognized (?:parameter|field|argument)|"
                         r"extra (?:inputs|fields)", re.I)
# Connections (API address, model) and the optional parts they refused: requests go without them.
REFUSED: dict[tuple[str, str], set[str]] = {}
# Connections that refused a streamed request: they get plain ones.
NO_STREAM: set[tuple[str, str]] = set()
STREAM_REFUSED = {400, 404, 405, 415, 422, 501}
# Connections where structured output spent the whole output on thinking and never got to the answer (GLM behind a
# gateway): they get the schema in the prompt.
NO_STRUCTURED: set[tuple[str, str]] = set()


def _refused(e: Exception, present: list[str]) -> str | None:
    """The optional part of the request a gateway refused: the one its error names, else the least needed one
    sent. None - not a refusal of a feature (or nothing left to drop)."""
    if not present or not isinstance(e, ApiError) or e.status_code not in (400, 422):
        return None
    text = f"{e.message} {e.body}".lower()
    if not UNSUPPORTED.search(text):
        return None
    named = [f for f in present if any(w in text for w in OPTIONAL[f])]
    return (named or present)[0]


class FeatureRefused(ProviderError):
    """The gateway does not take structured output: the caller asks with the schema in the prompt."""


def thought_out(reply: Reply) -> bool:
    """The output limit was spent on thinking before any answer."""
    return reply.stop == "max_tokens" and not reply.text and any(b.get("type") == "thinking" for b in reply.content)


PROGRESS_EVERY = 15     # seconds between the progress reports of a stream (Request.progress)


def response_format(schema) -> dict:
    return {"type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": False}}


class ChatProvider:
    def __init__(self, client, features: set[str]):
        self._client = client
        self.features = set(features)

    def client(self):
        return self._client

    def _key(self, model: str) -> tuple[str, str]:
        return str(getattr(self._client, "base_url", "")), model

    def _on(self, feature: str, model: str) -> bool:
        return feature not in REFUSED.get(self._key(model), ())

    @staticmethod
    def _sent(p: dict) -> list[str]:
        """The optional parts the request carries - what a refusal may be about - the least needed first."""
        has = {"tool_choice": "tool_choice" in p, "parallel": "parallel_tool_calls" in p,
               "effort": "reasoning_effort" in p, "structured": "response_format" in p}
        return [f for f in DROP_ORDER if has[f]]

    def _params(self, req: Request, schema=None) -> dict:
        system = req.system + (f"\n\n{req.context}" if req.context else "")
        messages = fill_empty(req.messages)
        if req.keep_images:
            messages = trim_history(messages, req.keep_images)     # old screenshots are useless once the page moved on
        p: dict = {"model": req.model, "max_tokens": req.max_tokens, "messages": messages_of(system, messages)}
        if "effort" in self.features and EFFORT.get(req.effort) and self._on("effort", req.model):
            p["reasoning_effort"] = EFFORT[req.effort]
        if req.tools:
            p["tools"] = tools_of(req.tools)
            if self._on("tool_choice", req.model):
                p["tool_choice"] = "auto"
            if req.one_tool and self._on("parallel", req.model):
                p["parallel_tool_calls"] = False
        if schema is not None:
            p["response_format"] = response_format(schema)
        return p

    def _streams(self, req: Request) -> bool:
        return req.stream and self._key(req.model) not in NO_STREAM

    async def _call(self, p: dict, req: Request, stream: bool) -> dict:
        if not stream:
            return await self.client().complete(p, timeout=req.timeout)
        told = [time.monotonic()]

        def on_delta(thought: int, said: int) -> bool:
            # A model that thinks past think_limit characters with no answer begun is given up there - what came,
            # cut off (GLM behind a gateway drafts every edit in its thinking until the output limit).
            if req.progress and time.monotonic() - told[0] >= PROGRESS_EVERY:
                told[0] = time.monotonic()
                req.progress(thought, said)
            return bool(req.think_limit and not said and thought > req.think_limit)
        return await self.client().complete(p, stream=True, timeout=req.timeout,
                                            on_delta=on_delta if (req.think_limit or req.progress) else None)

    async def _send(self, req: Request, schema=None) -> Reply:
        """The request; a part of it the gateway refuses is dropped and the request sent again. Remembered for the
        connection only when a request without it went through: a failure for another reason drops nothing for
        good. Structured output refused - FeatureRefused: the caller puts the schema in the prompt. A gateway that
        refuses streams gets the plain request (and from now on)."""
        key, dropped = self._key(req.model), set()
        stream = self._streams(req)
        try:
            while True:
                p = self._params(req, schema)
                try:
                    resp = await self._call(p, req, stream)
                    break
                except ApiError as e:
                    part = _refused(e, [f for f in self._sent(p) if f not in dropped])
                    if part is None:
                        if stream and e.status_code in STREAM_REFUSED:
                            NO_STREAM.add(key)
                            stream = False
                            continue
                        raise
                    if part == "structured":
                        NO_STRUCTURED.add(key)
                        raise FeatureRefused(error_text(e), status=e.status_code) from e
                    dropped.add(part)
                    REFUSED.setdefault(key, set()).add(part)
        except Exception as e:
            if dropped and not isinstance(e, FeatureRefused):
                REFUSED[key] -= dropped               # they were not the trouble: they stay on
            if isinstance(e, ProviderError):
                raise
            if is_error(e):
                raise ProviderError(error_text(e), status=getattr(e, "status_code", None)) from e
            raise
        return reply_of(resp)

    async def chat(self, req: Request) -> Reply:
        return await self._send(req)

    def structured(self, model: str) -> bool:
        return "structured" in self.features and self._key(model) not in NO_STRUCTURED

    async def parse(self, req: Request, schema) -> Reply:
        """Server-side structured output: the answer is validated by the caller (llm.parse), a cut-off one keeps
        its stop reason and text. An answer that never got past thinking turns structured output off for the
        connection (NO_STRUCTURED)."""
        reply = await self._send(req, schema)
        if thought_out(reply):
            NO_STRUCTURED.add(self._key(req.model))
        return reply
