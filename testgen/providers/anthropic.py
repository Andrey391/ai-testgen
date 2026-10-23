"""Requests to the model through the Anthropic Messages API - every feature the studio was built on.

Features (llm.Model.features) a model may lack - the studio then does the same on its side:

    cache            prompt caching (a breakpoint on tools + system, automatic caching of the conversation)
    context_editing  old screenshots are cleared server-side (clear_tool_uses); off: the studio cuts them
    effort           output_config.effort
    fallbacks        fallbacks="default": a declined request is re-run on the provider's fallback model
    structured       messages.parse with a pydantic output format; off: JSON schema in the prompt

A gateway that refuses one of them, or tool_choice ("... is not supported"), gets the request again without it
(OPTIONAL, REFUSED); structured output refused - the schema goes into the prompt.
"""
from __future__ import annotations

import re
import time

import anthropic
import httpx2
import pydantic
from anthropic.lib._parse._response import parse_beta_response
from anthropic.lib._parse._transform import transform_schema
from anthropic.types.beta import BetaMessage

from .base import ProviderError, Reply, Request, fill_empty, trim_history, usage_dict

FEATURES = ("cache", "context_editing", "effort", "fallbacks", "structured")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CONTEXT_BETA = "context-management-2025-06-27"
CLEAR_TOOL_USES = "clear_tool_uses_20250919"
EPHEMERAL = {"type": "ephemeral"}
STOP = {"tool_use": "tool", "end_turn": "end", "stop_sequence": "end", "pause_turn": "end",
        "max_tokens": "max_tokens", "refusal": "refusal", "model_context_window_exceeded": "max_tokens"}


def _dump(block) -> dict:
    if isinstance(block, dict):
        return dict(block)
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True, mode="json")
    return {k: v for k, v in vars(block).items() if v is not None}


def _lenient(data: dict) -> dict:
    """A gateway or a server of its own may answer with blocks the strict BetaMessage rejects,
    e.g. a thinking block with "signature": null: they are fixed before validation."""
    content = []
    for b in data.get("content") or []:
        if isinstance(b, dict) and b.get("type") == "thinking":
            b = {**b, "thinking": b.get("thinking") or "", "signature": b.get("signature") or ""}
        content.append(b)
    return {**data, "content": content}


NO_KEY ="API ИИ: неверный или не заданный API-ключ (укажите его в «Проект → Модель»)."
NO_CREDITS = ("API ИИ: на балансе аккаунта, к которому относится API-ключ проекта, закончились кредиты. "
              "Пополните баланс в консоли Anthropic (Plans & Billing) или укажите ключ другого аккаунта "
              "в «Проект → Модель».")


def _no_credits(e: Exception) -> bool:
    """The API answers 400 invalid_request_error "credit balance is too low" (or a billing_error)."""
    body = getattr(e, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else {}
    kind = err.get("type", "") if isinstance(err, dict) else ""
    return kind == "billing_error" or "credit balance" in str(getattr(e, "message", "") or e).lower()


def error_text(e: Exception) -> str:
    if isinstance(e, anthropic.AuthenticationError) or (isinstance(e, TypeError) and "authentication" in str(e)):
        # The SDK raises the TypeError before sending anything when no credentials are set.
        return NO_KEY
    if isinstance(e, anthropic.APIStatusError) and _no_credits(e):
        return NO_CREDITS
    if isinstance(e, (anthropic.APITimeoutError, httpx2.TimeoutException)):     # the latter: a pause in a stream
        return "API ИИ: модель не ответила за отведённое время — повторите или выберите модель быстрее."
    if isinstance(e, anthropic.RateLimitError):
        return "API ИИ: превышен лимит запросов, повторите чуть позже."
    if isinstance(e, anthropic.APIStatusError):
        return f"Ошибка API ИИ {e.status_code}: {e.message}"
    if isinstance(e, (anthropic.APIConnectionError, httpx2.TransportError)):
        return "API ИИ: ошибка сети."
    return f"{type(e).__name__}: {e}"


def is_error(e: Exception) -> bool:
    return isinstance(e, (anthropic.APIError, httpx2.TransportError))         or (isinstance(e, TypeError) and "authentication" in str(e))


def _context_editing_failed(e: Exception) -> bool:
    """A gateway (e.g. LiteLLM) took context_management but failed on it - its polyfill does not
    know image blocks - instead of answering that the feature is unsupported."""
    if not isinstance(e, anthropic.APIStatusError):
        return False
    text = f"{getattr(e, 'message', '')} {getattr(e, 'body', '')}".lower()
    return any(k in text for k in ("context_management", "context-management", CLEAR_TOOL_USES))


# A gateway of the project's own (LiteLLM over an OpenAI Responses API and the like) may refuse a part of a request
# the studio can do without: "tool_choice type 'auto' is not supported", "text.format type 'json_schema' is not
# supported", "Extra inputs are not permitted". The part is dropped and the request sent again, whatever the part.
# What names each optional part in such an error:
OPTIONAL = {
    "structured": ("json_schema", "output_format", "text.format", "response_format", "structured output"),
    "tool_choice": ("tool_choice",),
    "context_editing": ("context_management", "context-management", "clear_tool_uses"),
    "effort": ("effort", "output_config", "reasoning"),
    "fallbacks": ("fallbacks",),
    "cache": ("cache_control",),
}
# The order a refusal that names none of them drops them: the least needed first.
DROP_ORDER = ("fallbacks", "effort", "cache", "context_editing", "tool_choice", "structured")
UNSUPPORTED = re.compile(r"not (?:be )?supported|unsupported|feature_not_supported|not allowed|not permitted|"
                         r"unknown (?:parameter|field|argument)|unrecognized (?:parameter|field|argument)|"
                         r"extra (?:inputs|fields)", re.I)
# Connections (API address, model) and the optional parts they refused: requests go without them.
REFUSED: dict[tuple[str, str], set[str]] = {}


def _refused(e: Exception, present: list[str]) -> str | None:
    """The optional part of the request a gateway refused: the one its error names, else the least needed one
    sent. None - not a refusal of a feature (or nothing left to drop)."""
    if not present or not isinstance(e, anthropic.APIStatusError) or e.status_code not in (400, 422):
        return None
    text = f"{getattr(e, 'message', '')} {getattr(e, 'body', '')}".lower()
    if not UNSUPPORTED.search(text):
        return None
    named = [f for f in present if any(w in text for w in OPTIONAL[f])]
    return (named or present)[0]


class FeatureRefused(ProviderError):
    """The gateway does not take structured output: the caller asks with the schema in the prompt."""


# Connections (API address, model) where context editing failed: the studio cuts old screenshots itself.
NO_CONTEXT_EDITING: set[tuple[str, str]] = set()
# Connections (a gateway of the project's own) that refused a streamed request: they get plain ones.
NO_STREAM: set[tuple[str, str]] = set()
STREAM_REFUSED = {400, 404, 405, 415, 422, 501}
# Connections (a gateway of the project's own) where structured output spent the whole output on thinking and
# never got to the answer (GLM behind a gateway): they get the schema in the prompt.
NO_STRUCTURED: set[tuple[str, str]] = set()


def thought_out(reply: Reply) -> bool:
    """The output limit was spent on thinking before any answer."""
    return reply.stop == "max_tokens" and not reply.text and any(b.get("type") == "thinking" for b in reply.content)


def _stalled_in_thinking(stream) -> BetaMessage | None:
    """A stream that went quiet while the model was still thinking: what came, as cut off there."""
    try:
        msg = stream.current_message_snapshot
    except Exception:
        return None
    blocks = list(getattr(msg, "content", None) or [])
    if any(getattr(b, "type", "") == "text" and getattr(b, "text", "") for b in blocks)             or not any(getattr(b, "type", "") == "thinking" for b in blocks):
        return None
    return msg.model_copy(update={"stop_reason": "max_tokens"})


PROGRESS_EVERY = 15     # seconds between the progress reports of a stream (Request.progress)


async def _final(stream, req: Request) -> BetaMessage:
    """The answer of a stream. With Request.think_limit or progress it is read event by event: a model that thinks
    past think_limit characters with no answer begun is given up there - what came, cut off (GLM behind a gateway
    drafts every edit in its thinking until the output limit, minutes per request); progress hears how far it is."""
    if not (req.think_limit or req.progress):
        return await stream.get_final_message()
    thought = said = 0
    told = time.monotonic()
    async for ev in stream:
        if getattr(ev, "type", "") != "content_block_delta":
            continue
        d = ev.delta
        kind = getattr(d, "type", "")
        if kind == "thinking_delta":
            thought += len(d.thinking or "")
        elif kind in ("text_delta", "input_json_delta"):
            said += len(getattr(d, "text", "") or getattr(d, "partial_json", "") or "")
        if req.think_limit and not said and thought > req.think_limit:
            msg = _stalled_in_thinking(stream)
            if msg is not None:
                return msg
        if req.progress and time.monotonic() - told >= PROGRESS_EVERY:
            told = time.monotonic()
            req.progress(thought, said)
    return await stream.get_final_message()


def _timeout(req: Request) -> dict:
    """The request's own wait: for a stream - the longest pause in it, so a long answer still arriving is not cut."""
    return {"timeout": anthropic.Timeout(req.timeout, connect=min(req.timeout, 30.0))} if req.timeout else {}


class AnthropicProvider:
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
    def _sent(p: dict, structured: bool) -> list[str]:
        """The optional parts the request carries - what a refusal may be about - the least needed first."""
        has = {"tool_choice": "tool_choice" in p, "context_editing": "context_management" in p,
               "effort": "effort" in (p.get("output_config") or {}), "fallbacks": "fallbacks" in p,
               "cache": "cache_control" in p or (isinstance(p.get("system"), list)
                                                 and any("cache_control" in b for b in p["system"])),
               "structured": structured}
        return [f for f in DROP_ORDER if has[f]]

    def _params(self, req: Request) -> tuple[dict, list[str]]:
        cache = req.cache and "cache" in self.features and self._on("cache", req.model)
        if cache:
            system = [{"type": "text", "text": req.system, "cache_control": EPHEMERAL}]
            if req.context:
                # A breakpoint of its own: the same context (requirements) in every request of a job.
                system = [{"type": "text", "text": req.system},
                          {"type": "text", "text": req.context, "cache_control": EPHEMERAL}]
        else:
            system = req.system + (f"\n\n{req.context}" if req.context else "")
        messages = fill_empty(req.messages)
        betas: list[str] = []
        p = {"model": req.model, "max_tokens": req.max_tokens, "system": system}
        if "effort" in self.features and req.effort and self._on("effort", req.model):
            p["output_config"] = {"effort": req.effort}
        if "fallbacks" in self.features and self._on("fallbacks", req.model):
            p["fallbacks"] = "default"
            betas.append(FALLBACK_BETA)
        if req.tools:
            p["tools"] = req.tools
            if self._on("tool_choice", req.model):
                p["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": req.one_tool}
        if req.keep_images:
            if "context_editing" in self.features and self._key(req.model) not in NO_CONTEXT_EDITING \
                    and self._on("context_editing", req.model):
                betas.append(CONTEXT_BETA)
                # Old screenshots/snapshots are useless once the page moved on.
                p["context_management"] = {"edits": [{
                    "type": CLEAR_TOOL_USES,
                    "trigger": {"type": "input_tokens", "value": 40000},
                    "keep": {"type": "tool_uses", "value": req.keep_images},
                    "clear_at_least": {"type": "input_tokens", "value": 8000},
                }]}
            else:
                messages = trim_history(messages, req.keep_images)
        if cache and req.cache_all:
            p["cache_control"] = EPHEMERAL
        p["messages"] = messages
        return p, betas

    def _reply(self, resp) -> Reply:
        u = getattr(resp, "usage", None)
        return Reply(content=[_dump(b) for b in resp.content or []],
                     stop=STOP.get(getattr(resp, "stop_reason", "") or "", "end"),
                     model=getattr(resp, "model", "") or "",
                     usage=usage_dict(getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0),
                                      getattr(u, "cache_creation_input_tokens", 0),
                                      getattr(u, "cache_read_input_tokens", 0)),
                     parsed=getattr(resp, "parsed_output", None))

    async def _send(self, req: Request, call, structured: bool = False) -> Reply:
        """The request; a part of it the gateway refuses is dropped and the request sent again. Remembered for the
        connection only when a request without it went through: a failure for another reason drops nothing for
        good. Structured output refused - FeatureRefused: the caller puts the schema in the prompt."""
        p, betas = self._params(req)
        key, dropped = self._key(req.model), set()
        try:
            while True:
                try:
                    resp = await call(p, betas)
                    break
                except anthropic.APIStatusError as e:
                    if "context_management" in p and _context_editing_failed(e):
                        NO_CONTEXT_EDITING.add(key)
                    else:
                        part = _refused(e, [f for f in self._sent(p, structured) if f not in dropped])
                        if part is None:
                            raise
                        if part == "structured":
                            NO_STRUCTURED.add(key)
                            raise FeatureRefused(error_text(e), status=e.status_code) from e
                        dropped.add(part)
                        REFUSED.setdefault(key, set()).add(part)
                    p, betas = self._params(req)      # now without the refused part
        except Exception as e:
            if dropped and not isinstance(e, FeatureRefused):
                REFUSED[key] -= dropped               # they were not the trouble: they stay on
            if isinstance(e, ProviderError):
                raise
            if is_error(e):
                raise ProviderError(error_text(e), status=getattr(e, "status_code", None)) from e
            raise
        return self._reply(resp)

    def _streams(self, req: Request) -> bool:
        return req.stream and self._key(req.model) not in NO_STREAM and hasattr(self.client().beta.messages, "stream")

    async def _streamed(self, req: Request, call, plain, structured: bool = False) -> Reply:
        """A streamed request; a gateway that refuses streams gets the plain one (and from now on)."""
        try:
            return await self._send(req, call, structured)
        except ProviderError as e:
            if isinstance(e, FeatureRefused) or e.status not in STREAM_REFUSED:
                raise
            NO_STREAM.add(self._key(req.model))
            return await self._send(req, plain, structured)

    async def chat(self, req: Request) -> Reply:
        def plain(p, betas):
            return self.client().beta.messages.create(**p, **({"betas": betas} if betas else {}), **_timeout(req))

        async def streamed(p, betas):
            async with self.client().beta.messages.stream(**p, **({"betas": betas} if betas else {}),
                                                          **_timeout(req)) as s:
                return await _final(s, req)
        if self._streams(req):
            return await self._streamed(req, streamed, plain)
        return await self._send(req, plain)

    def structured(self, model: str) -> bool:
        return "structured" in self.features and self._key(model) not in NO_STRUCTURED

    async def parse(self, req: Request, schema) -> Reply:
        """Server-side structured output; the caller falls back to the prompt when it is off.
        The SDK validates the answer itself and raises on one cut off by max_tokens, losing the
        stop reason and the usage: the raw response is read and validated here instead.
        An answer that never got past thinking turns structured output off for the connection (NO_STRUCTURED)."""
        reply = await self._parse(req, schema)
        if thought_out(reply):
            NO_STRUCTURED.add(self._key(req.model))
        return reply

    async def _parse(self, req: Request, schema) -> Reply:
        def parsed(msg):
            try:
                return parse_beta_response(response=msg, output_format=schema)
            except pydantic.ValidationError:
                return msg          # no parsed_output: the caller sees the stop reason and the text

        async def call(p, betas):
            kw = {**p, "output_format": schema, **({"betas": betas} if betas else {}), **_timeout(req)}
            api = self.client().beta.messages
            raw_api = getattr(api, "with_raw_response", None)
            if raw_api is None:
                return await api.parse(**kw)
            raw = await raw_api.parse(**kw)
            return parsed(BetaMessage.model_validate(_lenient(raw.http_response.json())))

        async def streamed(p, betas):
            # The schema as output_config, not output_format: the SDK would validate a cut-off answer itself
            # and raise, losing what came; it is validated here.
            fmt = {"type": "json_schema", "schema": transform_schema(pydantic.TypeAdapter(schema).json_schema())}
            kw = {**p, "output_config": {**(p.get("output_config") or {}), "format": fmt},
                  **({"betas": betas} if betas else {}), **_timeout(req)}
            async with self.client().beta.messages.stream(**kw) as s:
                try:
                    msg = await _final(s, req)
                except httpx2.TimeoutException:
                    msg = _stalled_in_thinking(s)
                    if msg is None:
                        raise
            return parsed(msg) if isinstance(msg, BetaMessage) and getattr(msg, "parsed_output", None) is None else msg
        if self._streams(req):
            return await self._streamed(req, streamed, call, structured=True)
        return await self._send(req, call, structured=True)
