"""`decapod web`: the monitor in a browser, via textual-serve, behind a small guard.

textual-serve itself has no authentication and accepts cross-origin WebSockets, so any web
page the operator visits could drive the dashboard (approve actions, kill the agent). The
guard requires a random per-launch token (given once in the printed URL, then kept in a
SameSite=Strict, HttpOnly cookie), checks the Host header (against DNS rebinding) and
rejects WebSocket and download requests from any other Origin.
"""

from __future__ import annotations

import secrets
from typing import Any

COOKIE = "decapod_token"


def allowed_hosts(host: str, port: int) -> set[str]:
    names = {host}
    if host in ("127.0.0.1", "localhost", "::1"):
        names |= {"127.0.0.1", "localhost", "[::1]"}
    return {f"{name}:{port}" for name in names} | (names if port == 80 else set())


def make_guard(token: str, host: str, port: int) -> Any:
    from aiohttp import web

    hosts = allowed_hosts(host, port)
    origins = {f"http://{h}" for h in hosts} | {f"https://{h}" for h in hosts}

    @web.middleware
    async def guard(request: web.Request, handler: Any) -> web.StreamResponse:
        if request.host not in hosts:
            return web.Response(status=403, text="Forbidden: unexpected Host header.")
        if request.path.startswith("/static/"):
            return await handler(request)
        if request.path == "/":
            supplied = request.query.get("token") or request.cookies.get(COOKIE)
            if not supplied or not secrets.compare_digest(supplied, token):
                return web.Response(
                    status=403,
                    text="Forbidden: open the exact URL printed "
                    "by `decapod web` (it contains a token).",
                )
            response = await handler(request)
            response.set_cookie(COOKIE, token, httponly=True, samesite="Strict")
            return response
        origin = request.headers.get("Origin")
        if origin is not None and origin not in origins:
            return web.Response(status=403, text="Forbidden: cross-origin request.")
        cookie = request.cookies.get(COOKIE)
        if not cookie or not secrets.compare_digest(cookie, token):
            return web.Response(status=403, text="Forbidden: missing session token.")
        return await handler(request)

    return guard


def make_server(command: str, host: str, port: int, token: str) -> Any:
    from textual_serve.server import Server

    class GuardedServer(Server):
        async def _make_app(self) -> Any:
            app = await super()._make_app()
            app.middlewares.append(make_guard(token, host, port))
            return app

    return GuardedServer(command, host=host, port=port, title="Decapod")


def new_token() -> str:
    return secrets.token_urlsafe(24)
