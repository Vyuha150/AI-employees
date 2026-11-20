"""ASGI adapter (served by uvicorn) for HttpApi, with Server-Sent Events for realtime.

- All routes go through HttpApi.handle (run in a worker thread; domain code is synchronous).
- GET /v1/events/stream: SSE. Authenticated with the Authorization header (use a fetch-based
  EventSource client). Tokens in query strings are NOT accepted (they leak into logs). Resume with
  the Last-Event-ID header or ?after=<cursor>. Each connection lasts at most `sse_max_seconds`;
  clients reconnect with the last id.
- CORS: only origins listed in CC_CORS_ORIGINS. Security headers on every response.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .http_api import ApiError, HttpApi, HttpRequest, envelope, json_bytes, map_exception

MAX_BODY = 1_000_000
SECURITY_HEADERS = [(b"x-content-type-options", b"nosniff"), (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY")]


class AsgiApp:
    def __init__(self, http: HttpApi, *, cors_origins: tuple[str, ...] = (), sse_poll_seconds: float = 1.0,
                 sse_max_seconds: float = 25.0, heartbeat_seconds: float = 15.0):
        self.http = http
        self.cors = frozenset(o.rstrip("/") for o in cors_origins if o)
        self.poll, self.max_seconds, self.heartbeat = sse_poll_seconds, sse_max_seconds, heartbeat_seconds

    def _cors_headers(self, headers: dict[str, str]) -> list[tuple[bytes, bytes]]:
        origin = headers.get("origin", "").rstrip("/")
        if origin and origin in self.cors:
            return [(b"access-control-allow-origin", origin.encode()), (b"vary", b"Origin"),
                    (b"access-control-allow-headers", b"authorization, content-type, x-organization, x-request-id, "
                                                      b"last-event-id"),
                    (b"access-control-allow-methods", b"GET, POST, PATCH, OPTIONS"),
                    (b"access-control-max-age", b"600")]
        return []

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        cors = self._cors_headers(headers)
        if scope["method"] == "OPTIONS":
            await self._send(send, 204 if cors else 403, None, cors)
            return
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if len(body) > MAX_BODY:
                await self._send(send, 413, envelope(None, {}, ApiError(413, "PAYLOAD_TOO_LARGE", "body too large")),
                                 cors)
                return
            if not msg.get("more_body"):
                break
        query = {}
        for part in scope.get("query_string", b"").decode().split("&"):
            if part:
                k, _, v = part.partition("=")
                from urllib.parse import unquote_plus
                query[unquote_plus(k)] = unquote_plus(v)
        client = scope.get("client") or (None, None)
        req = HttpRequest(scope["method"], scope["path"], query, headers, body, client[0])
        if scope["path"] == "/v1/events/stream" and scope["method"] == "GET":
            await self._sse(req, receive, send, cors)
            return
        resp = await asyncio.to_thread(self.http.handle, req)
        extra = cors + [(k.encode(), v.encode()) for k, v in resp.headers.items()]
        await self._send(send, resp.status, resp.body, extra)

    async def _send(self, send: Any, status: int, body: Any, extra: list[tuple[bytes, bytes]]) -> None:
        payload = b"" if body is None else json_bytes(body)
        hdrs = [(b"content-type", b"application/json")] + SECURITY_HEADERS + extra
        await send({"type": "http.response.start", "status": status, "headers": hdrs})
        await send({"type": "http.response.body", "body": payload})

    async def _sse(self, req: HttpRequest, receive: Any, send: Any, cors: list) -> None:
        if "access_token" in req.query or "token" in req.query:
            err = ApiError(422, "VALIDATION_FAILED", "tokens are not accepted in the URL; send Authorization")
            await self._send(send, 422, envelope(None, {}, err), cors)
            return
        try:
            principal = await asyncio.to_thread(self.http._principal, req)
            route = next(r for r in self.http.routes if r.path == "/v1/events")
            await asyncio.to_thread(self.http._limits, route, principal)
            cursor = int(req.headers.get("last-event-id") or req.query.get("after") or 0)
        except Exception as exc:
            err = map_exception(exc)
            await self._send(send, err.status, envelope(None, {}, err), cors + [(k.encode(), v.encode())
                                                                               for k, v in err.headers.items()])
            return
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream"), (b"x-accel-buffering", b"no")]
                    + SECURITY_HEADERS + cors})
        disconnected = asyncio.Event()

        async def watch() -> None:
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    disconnected.set()
                    return
        watcher = asyncio.create_task(watch())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.max_seconds
        last_beat = loop.time()
        try:
            await send({"type": "http.response.body", "body": b"retry: 2000\n\n", "more_body": True})
            while not disconnected.is_set() and loop.time() < deadline:
                page = await asyncio.to_thread(self.http.api.events_since, principal, after=cursor, limit=100)
                for item in page["items"]:
                    chunk = f"id: {item['id']}\nevent: {item['event_type']}\ndata: {json.dumps(item)}\n\n"
                    await send({"type": "http.response.body", "body": chunk.encode(), "more_body": True})
                cursor = page["next_cursor"]
                if loop.time() - last_beat >= self.heartbeat:
                    await send({"type": "http.response.body", "body": b": keep-alive\n\n", "more_body": True})
                    last_beat = loop.time()
                try:
                    await asyncio.wait_for(disconnected.wait(), timeout=self.poll)
                except asyncio.TimeoutError:
                    pass
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            watcher.cancel()
