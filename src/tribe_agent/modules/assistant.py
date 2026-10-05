"""Assistant module (platform) — HTTP service in front of `opencode serve`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from tribe_agent.core.config import AppConfig
from tribe_agent.core.module import Module

DEFAULT_AGENT = "assistant"
PRESENCE_HEARTBEAT_SECONDS = 60


def _auth_tuple(password: str) -> tuple[str, str] | None:
    return ("opencode", password) if password else None


def _build_parts(text: str, context: dict[str, Any] | None) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if context:
        parts.append(
            {"type": "text", "text": f"[CONTEXT]\n{json.dumps(context, ensure_ascii=False)}"}
        )
    parts.append({"type": "text", "text": text})
    return parts


class OpencodeClient:
    def __init__(self, base_url: str, password: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = _auth_tuple(password)

    def _client(self, timeout: float | None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, auth=self.auth, timeout=timeout)

    async def create_session(self, title: str = "assistant-chat") -> str:
        async with self._client(30) as client:
            resp = await client.post("/session", json={"title": title})
            resp.raise_for_status()
            return resp.json().get("id", "")

    async def delete_session(self, session_id: str) -> None:
        async with self._client(30) as client:
            resp = await client.delete(f"/session/{session_id}")
            if resp.status_code not in (200, 404):
                resp.raise_for_status()

    async def send_message(
        self, session_id: str, text: str, context: dict[str, Any] | None, timeout: float
    ) -> str:
        body = {"parts": _build_parts(text, context), "agent": DEFAULT_AGENT}
        async with self._client(timeout) as client:
            resp = await client.post(f"/session/{session_id}/message", json=body)
            resp.raise_for_status()
            data = resp.json()
        texts = [p.get("text", "") for p in data.get("parts", []) if p.get("type") == "text"]
        return "\n".join(t for t in texts if t.strip())

    async def stream(
        self, session_id: str, text: str, context: dict[str, Any] | None
    ) -> AsyncIterator[dict[str, Any]]:
        body = {"parts": _build_parts(text, context), "agent": DEFAULT_AGENT}
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._client(None) as client, client.stream("GET", "/event") as resp:
            reader = asyncio.create_task(_read_sse(resp, queue))
            try:
                post = await client.post(f"/session/{session_id}/prompt_async", json=body)
                post.raise_for_status()
                while True:
                    event = await queue.get()
                    props = event.get("properties") or {}
                    if props.get("sessionID") and props.get("sessionID") != session_id:
                        continue
                    etype = event.get("type")
                    if etype == "message.part.updated":
                        part = props.get("part") or {}
                        if part.get("type") == "text":
                            yield {"type": "delta", "text": part.get("text", "")}
                    elif etype == "session.idle":
                        yield {"type": "done"}
                        return
                    elif etype == "session.error":
                        yield {"type": "error", "error": str(props.get("error", "session error"))}
                        return
            finally:
                reader.cancel()


async def _read_sse(resp: httpx.Response, queue: asyncio.Queue[dict[str, Any]]) -> None:
    with contextlib.suppress(Exception):
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            payload = data.get("payload", data)
            if isinstance(payload, dict):
                queue.put_nowait(payload)


def make_app(config: AppConfig):
    import jwt
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    settings = config.module_config("assistant")
    client = OpencodeClient(config.opencode.url, config.opencode.password)
    rate: dict[str, list[float]] = {}
    rate_limit = int(settings.get("rate_limit", 30))
    timeout = float(settings.get("timeout", 180))

    app = FastAPI(title="Tribe Assistant")

    def extract_user_id(request: Request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        try:
            payload = jwt.decode(
                auth[7:], config.community.jwt_secret, algorithms=[config.community.jwt_algorithm]
            )
        except jwt.PyJWTError:
            return None
        if payload.get("type") != "access":
            return None
        return payload.get("sub")

    def check_rate(user_id: str) -> bool:
        now = time.monotonic()
        hits = [t for t in rate.get(user_id, []) if now - t < 60]
        rate[user_id] = hits
        if len(hits) >= rate_limit:
            return False
        hits.append(now)
        return True

    def enrich(context: dict[str, Any], user_id: str) -> dict[str, Any]:
        result = dict(context or {})
        result["user_id"] = user_id
        result.setdefault("feedbackWorkspaceId", settings.get("feedback_workspace_id", ""))
        result.setdefault("feedbackCommunityId", settings.get("feedback_community_id", ""))
        return result

    def guard(request: Request):
        user_id = extract_user_id(request)
        if not user_id:
            return None, JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not check_rate(user_id):
            return None, JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        return user_id, None

    @app.post("/assistant/session")
    async def session(request: Request) -> JSONResponse:
        _, error = guard(request)
        if error:
            return error
        try:
            session_id = await client.create_session()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"session_id": session_id})

    @app.post("/assistant/message")
    async def message(request: Request) -> JSONResponse:
        user_id, error = guard(request)
        if error:
            return error
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "Empty message"}, status_code=400)
        context = enrich(body.get("context") or {}, user_id)
        session_id = body.get("session_id") or ""
        created = False
        try:
            if not session_id:
                session_id = await client.create_session()
                created = True
            reply = await client.send_message(session_id, text, context, timeout)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=502)
        result: dict[str, Any] = {"session_id": session_id, "reply": reply}
        if created:
            result["created"] = True
        return JSONResponse(result)

    @app.post("/assistant/stream", response_model=None)
    async def stream(request: Request):
        user_id, error = guard(request)
        if error:
            return error
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "Empty message"}, status_code=400)
        context = enrich(body.get("context") or {}, user_id)
        session_id = body.get("session_id") or ""

        async def gen():
            sid = session_id
            try:
                if not sid:
                    sid = await client.create_session()
                yield f"data: {json.dumps({'type': 'session', 'session_id': sid})}\n\n"
                async for event in client.stream(sid, text, context):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event.get("type") in ("done", "error"):
                        break
            except Exception as exc:  # noqa: BLE001
                yield f"data: {json.dumps({'type': 'error', 'error': str(exc)}, ensure_ascii=False)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/assistant/reset")
    async def reset(request: Request) -> JSONResponse:
        _, error = guard(request)
        if error:
            return error
        body = await request.json()
        session_id = (body.get("session_id") or "").strip()
        if not session_id:
            return JSONResponse({"error": "Missing session_id"}, status_code=400)
        try:
            await client.delete_session(session_id)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"ok": True})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


class AssistantModule(Module):
    id = "assistant"

    def service_app(self, config: AppConfig):
        try:
            return make_app(config)
        except ImportError:
            return None
