"""OpenAI-compatible Chat Completions: vLLM and Ollama (a model on your own server),
Yandex AI Studio (https://llm.api.cloud.yandex.net/v1, model gpt://<folder>/yandexgpt/latest),
and any other server that speaks POST /chat/completions with tools.

Differences from the neutral (Anthropic) format handled here:
- tool results are "tool" messages right after the assistant's tool_calls; they cannot
  carry images, so a screenshot of a tool result goes into the next user message;
- a model without vision gets a note instead of each image;
- weaker models sometimes write the call as text (<tool_call>{...}</tool_call> or a JSON
  block) instead of using tool_calls: such text is read as the call.
"""
from __future__ import annotations

import json
import re
import uuid

import httpx

from .base import (ProviderError, Reply, Request, blocks, drop_images, loads_args, plain_schema, result_parts,
                   trim_history, usage_dict)

TIMEOUT = httpx.Timeout(180, connect=15)


def _image_part(b: dict) -> dict:
    src = b["source"]
    return {"type": "image_url", "image_url": {"url": f"data:{src.get('media_type', 'image/jpeg')};base64,{src['data']}"}}


def to_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        if m["role"] == "assistant":
            text, calls = [], []
            for b in blocks(m["content"]):
                if b.get("type") == "text" and b.get("text"):
                    text.append(b["text"])
                elif b.get("type") == "tool_use":
                    calls.append({"id": b["id"], "type": "function",
                                  "function": {"name": b["name"],
                                               "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
            msg: dict = {"role": "assistant", "content": "\n".join(text) or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        parts, images = [], []
        for b in blocks(m["content"]):
            t = b.get("type")
            if t == "tool_result":
                text, imgs = result_parts(b.get("content"))
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                            "content": ("ERROR: " if b.get("is_error") else "") + (text or "ok")})
                images += imgs
            elif t == "text":
                parts.append({"type": "text", "text": b["text"]})
            elif t == "image":
                parts.append(_image_part(b))
        if images:
            parts = ([{"type": "text", "text": "Screenshot for the tool result above:"}]
                     + [_image_part(i) for i in images] + parts)
        if parts:
            has_image = any(p["type"] == "image_url" for p in parts)
            out.append({"role": "user", "content": parts if has_image else "\n\n".join(p["text"] for p in parts)})
    return out


def to_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": plain_schema(t.get("input_schema"))}} for t in tools]


_TEXT_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>|```(?:json)?\s*(\{.*?\})\s*```", re.S)


def text_tool_call(text: str, tools: list[dict]) -> dict | None:
    """A tool call written as text by a model without a tool parser."""
    names = {t["name"] for t in tools}
    for m in _TEXT_CALL.finditer(text or ""):
        try:
            d = json.loads(m.group(1) or m.group(2))
        except ValueError:
            continue
        name = d.get("name") or d.get("tool") or d.get("function")
        args = d.get("arguments", d.get("parameters", d.get("input", {})))
        if name in names:
            return {"type": "tool_use", "id": f"call_{uuid.uuid4().hex[:12]}", "name": name,
                    "input": loads_args(args)}
    return None


class OpenAIProvider:
    kind = "openai"

    def __init__(self, cfg: dict, api_key: str, transport=None):
        self.cfg, self.id, self.api_key = cfg, cfg["id"], api_key
        self.transport = transport

    @property
    def structured(self) -> bool:
        return "json_schema" in (self.cfg.get("features") or [])

    def vision(self, model: str) -> bool:
        m = next((x for x in self.cfg.get("models") or [] if x.get("name") == model), {})
        return bool(m.get("vision", self.cfg.get("vision", True)))

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"{self.cfg.get('auth_scheme') or 'Bearer'} {self.api_key}"
        h.update(self.cfg.get("headers") or {})
        return h

    async def _post(self, body: dict) -> dict:
        url = (self.cfg.get("base_url") or "").rstrip("/") + "/chat/completions"
        if not url.startswith("http"):
            raise ProviderError(f"{self.cfg.get('title') or self.id}: не задан адрес сервера модели", retryable=False)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport,
                                         verify=self.cfg.get("verify", True)) as c:
                r = await c.post(url, json=body, headers=self._headers())
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.cfg.get('title') or self.id}: сеть — {type(e).__name__}: {e}") from e
        if r.status_code >= 400:
            raise ProviderError(f"{self.cfg.get('title') or self.id}: HTTP {r.status_code} — {r.text[:300]}",
                                status=r.status_code)
        try:
            return r.json()
        except ValueError as e:
            raise ProviderError(f"{self.cfg.get('title') or self.id}: ответ не JSON") from e

    def _body(self, req: Request) -> dict:
        messages = trim_history(req.messages, req.keep_images) if req.keep_images else req.messages
        if not self.vision(req.model):
            messages = drop_images(messages)
        body = {"model": req.model, "max_tokens": req.max_tokens,
                "messages": to_messages(req.system + (f"\n\n{req.context}" if req.context else ""), messages)}
        if req.tools:
            body["tools"] = to_tools(req.tools)
            body["tool_choice"] = "auto"
            if req.one_tool and "parallel_tool_calls" in (self.cfg.get("features") or ["parallel_tool_calls"]):
                body["parallel_tool_calls"] = False
        if self.cfg.get("temperature") is not None:
            body["temperature"] = self.cfg["temperature"]
        return body

    def _reply(self, data: dict, req: Request) -> Reply:
        choice = (data.get("choices") or [{}])[0]
        msg, finish = choice.get("message") or {}, choice.get("finish_reason") or ""
        content: list[dict] = []
        text = msg.get("content") or ""
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        calls = [{"type": "tool_use", "id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                  "name": (tc.get("function") or {}).get("name", ""),
                  "input": loads_args((tc.get("function") or {}).get("arguments"))}
                 for tc in msg.get("tool_calls") or []]
        if not calls and req.tools:
            call = text_tool_call(text, req.tools)
            if call:
                calls, text = [call], _TEXT_CALL.sub("", text).strip()
        if text:
            content.append({"type": "text", "text": text})
        content += calls[:1] if req.one_tool else calls
        u = data.get("usage") or {}
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        stop = "tool" if calls else {"length": "max_tokens", "content_filter": "refusal"}.get(finish, "end")
        return Reply(content=content, stop=stop, model=data.get("model") or req.model, provider=self.id,
                     usage=usage_dict(max(0, (u.get("prompt_tokens") or 0) - cached), u.get("completion_tokens"),
                                      0, cached))

    async def chat(self, req: Request) -> Reply:
        return self._reply(await self._post(self._body(req)), req)

    async def parse(self, req: Request, schema) -> Reply:
        body = self._body(req)
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": schema.__name__, "schema": schema.model_json_schema(), "strict": False}}
        return self._reply(await self._post(body), req)
