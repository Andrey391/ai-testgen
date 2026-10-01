"""GigaChat API (Sber): https://gigachat.devices.sberbank.ru/api/v1.

- Auth: the authorization key (Base64 of client id and secret, from the developer
  console) is exchanged for a 30-minute access token at the OAuth endpoint, scope
  GIGACHAT_API_PERS | GIGACHAT_API_B2B | GIGACHAT_API_CORP.
- Tools are "functions"; an answer calls at most one (the studio's agent makes one
  step per turn anyway). The assistant message with function_call carries
  functions_state_id, which must be sent back: it rides in the tool_use block's "_meta".
  A function result is a "function" message whose content is a JSON string.
- Images are uploaded to /files and attached to a USER message by id: one image per
  message, at most 10 per conversation. So only the latest screenshot is kept.
- Structured output: a single "answer" function with the schema, called by force.
- Sber's certificates are issued by the Russian Trusted Root CA: point "verify" at its
  bundle (or switch verification off on a test machine).
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

import httpx

from .base import ProviderError, Reply, Request, blocks, loads_args, plain_schema, result_parts, trim_history, \
    usage_dict

AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
BASE_URL = "https://gigachat.devices.sberbank.ru/api/v1"
TIMEOUT = httpx.Timeout(180, connect=15)
MAX_IMAGES = 1           # images kept in the conversation (the API allows 1 per message, 10 per chat)


class GigaChatProvider:
    kind = "gigachat"

    def __init__(self, cfg: dict, credentials: str, transport=None):
        self.cfg, self.id = cfg, cfg["id"]
        self.credentials = credentials
        self.transport = transport
        self._token, self._expires = "", 0.0
        self._files: dict[str, str] = {}       # sha1 of an image -> uploaded file id

    structured = True

    def vision(self, model: str) -> bool:
        m = next((x for x in self.cfg.get("models") or [] if x.get("name") == model), {})
        return bool(m.get("vision", self.cfg.get("vision", True)))

    def _client(self) -> httpx.AsyncClient:
        verify = self.cfg.get("verify", True)
        return httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport,
                                 verify=verify if verify not in ("", None) else True)

    def _title(self) -> str:
        return self.cfg.get("title") or "GigaChat"

    async def _auth(self, c: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        if not self.credentials:
            raise ProviderError(f"{self._title()}: не задан ключ авторизации (GIGACHAT_CREDENTIALS)", retryable=True)
        try:
            r = await c.post(self.cfg.get("auth_url") or AUTH_URL,
                             headers={"Authorization": f"Basic {self.credentials}", "RqUID": str(uuid.uuid4()),
                                      "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                             data={"scope": self.cfg.get("scope") or "GIGACHAT_API_PERS"})
        except httpx.HTTPError as e:
            raise ProviderError(f"{self._title()}: нет связи с сервером авторизации — {e}") from e
        if r.status_code >= 400:
            raise ProviderError(f"{self._title()}: авторизация не прошла (HTTP {r.status_code}) — {r.text[:200]}",
                                status=r.status_code)
        d = r.json()
        self._token = d["access_token"]
        exp = d.get("expires_at") or 0
        self._expires = exp / 1000 if exp > 1e11 else (exp or time.time() + 1500)
        return self._token

    async def _call(self, c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
        token = await self._auth(c)
        url = (self.cfg.get("base_url") or BASE_URL).rstrip("/") + path
        try:
            r = await c.request(method, url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                                **kw)
        except httpx.HTTPError as e:
            raise ProviderError(f"{self._title()}: сеть — {type(e).__name__}: {e}") from e
        if r.status_code == 401:
            self._token = ""
        if r.status_code >= 400:
            raise ProviderError(f"{self._title()}: HTTP {r.status_code} — {r.text[:300]}", status=r.status_code)
        return r.json()

    async def _upload(self, c: httpx.AsyncClient, image: dict) -> str:
        data = base64.b64decode(image["source"]["data"])
        key = hashlib.sha1(data).hexdigest()
        if key not in self._files:
            mime = image["source"].get("media_type", "image/jpeg")
            d = await self._call(c, "POST", "/files", files={"file": (f"screen.{mime.split('/')[-1]}", data, mime)},
                                 data={"purpose": "general"})
            self._files[key] = d["id"]
        return self._files[key]

    async def _messages(self, c: httpx.AsyncClient, req: Request) -> list[dict]:
        messages = trim_history(req.messages, req.keep_images or 1)
        vision = self.vision(req.model)
        # Only the latest images survive: the API limits images per conversation.
        budget = MAX_IMAGES if vision else 0
        pending_images: list[list[dict]] = []
        for m in reversed(messages):
            imgs = []
            for b in blocks(m["content"]):
                if b.get("type") == "image":
                    imgs.append(b)
                elif b.get("type") == "tool_result":
                    imgs += result_parts(b.get("content"))[1]
            keep = imgs[-budget:] if budget > 0 else []
            budget -= len(keep)
            pending_images.append(keep)
        pending_images.reverse()
        names = {b["id"]: b["name"] for m in messages if m["role"] == "assistant"
                 for b in blocks(m["content"]) if b.get("type") == "tool_use"}
        system = req.system + (f"\n\n{req.context}" if req.context else "")
        out: list[dict] = [{"role": "system", "content": system}] if system else []
        for m, images in zip(messages, pending_images):
            if m["role"] == "assistant":
                text = "\n".join(b["text"] for b in blocks(m["content"]) if b.get("type") == "text" and b.get("text"))
                call = next((b for b in blocks(m["content"]) if b.get("type") == "tool_use"), None)
                msg: dict = {"role": "assistant", "content": text}
                if call:
                    msg["function_call"] = {"name": call["name"], "arguments": call.get("input") or {}}
                    state = (call.get("_meta") or {}).get("functions_state_id")
                    if state:
                        msg["functions_state_id"] = state
                out.append(msg)
                continue
            texts = []
            for b in blocks(m["content"]):
                t = b.get("type")
                if t == "tool_result":
                    text, _ = result_parts(b.get("content"))
                    out.append({"role": "function", "name": names.get(b["tool_use_id"], "tool"),
                                "content": json.dumps({"error" if b.get("is_error") else "result": text or "ok"},
                                                      ensure_ascii=False)})
                elif t == "text":
                    texts.append(b["text"])
                elif t == "image" and not vision:
                    texts.append("[image omitted: this model does not see images]")
            if texts or images:
                msg = {"role": "user", "content": "\n\n".join(texts) or "Screenshot of the current page."}
                if images:
                    msg["attachments"] = [await self._upload(c, images[-1])]
                out.append(msg)
        return out

    def _body(self, req: Request, messages: list[dict]) -> dict:
        body = {"model": req.model, "messages": messages, "max_tokens": req.max_tokens}
        if req.tools:
            body["functions"] = [{"name": t["name"], "description": t.get("description", "")[:1000],
                                  "parameters": plain_schema(t.get("input_schema"))} for t in req.tools]
            body["function_call"] = "auto"
        if self.cfg.get("temperature") is not None:
            body["temperature"] = self.cfg["temperature"]
        return body

    def _reply(self, d: dict, req: Request) -> Reply:
        choice = (d.get("choices") or [{}])[0]
        msg, finish = choice.get("message") or {}, choice.get("finish_reason") or ""
        content: list[dict] = []
        if msg.get("content"):
            content.append({"type": "text", "text": msg["content"]})
        fc = msg.get("function_call")
        if fc:
            call = {"type": "tool_use", "id": f"call_{uuid.uuid4().hex[:12]}", "name": fc.get("name", ""),
                    "input": loads_args(fc.get("arguments"))}
            if msg.get("functions_state_id"):
                call["_meta"] = {"functions_state_id": msg["functions_state_id"]}
            content.append(call)
        u = d.get("usage") or {}
        cached = u.get("precached_prompt_tokens") or 0
        stop = "tool" if fc else {"length": "max_tokens", "blacklist": "refusal"}.get(finish, "end")
        return Reply(content=content, stop=stop, model=d.get("model") or req.model, provider=self.id,
                     usage=usage_dict(max(0, (u.get("prompt_tokens") or 0) - cached), u.get("completion_tokens"),
                                      0, cached))

    async def chat(self, req: Request) -> Reply:
        async with self._client() as c:
            body = self._body(req, await self._messages(c, req))
            return self._reply(await self._call(c, "POST", "/chat/completions", json=body), req)

    async def parse(self, req: Request, schema) -> Reply:
        """Structured output through a forced call of an "answer" function."""
        fn = {"name": "answer", "description": "Give the answer in this structure.",
              "input_schema": schema.model_json_schema()}
        async with self._client() as c:
            body = self._body(req, await self._messages(c, req))
            body["functions"] = [{"name": "answer", "description": fn["description"],
                                  "parameters": plain_schema(fn["input_schema"])}]
            body["function_call"] = {"name": "answer"}
            reply = self._reply(await self._call(c, "POST", "/chat/completions", json=body), req)
        call = next((b for b in reply.content if b.get("type") == "tool_use"), None)
        if call:
            reply.content = [{"type": "text", "text": json.dumps(call["input"], ensure_ascii=False)}]
        return reply
