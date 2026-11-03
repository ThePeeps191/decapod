"""A tiny forward proxy that every byte from the headless browser must pass through.

Chromium is launched with ``--proxy-server`` pointing here (and with the implicit loopback
bypass disabled), so redirects, sub-resources, XHR/fetch, WebSockets (ws:// and wss://) and
service requests all arrive as either ``CONNECT host:port`` or an absolute-form HTTP request.
For each one the proxy resolves the host itself, applies the outbound address policy to
*every* resolved address, and connects to the vetted IP — so there is no second DNS lookup
that a rebinding attack could poison.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from urllib.parse import urlsplit

from decapod.tools.builtin import web as web_mod

log = logging.getLogger(__name__)
MAX_HEAD = 64 * 1024


class FilteringProxy:
    def __init__(self, *, allow_private: bool, connect_timeout: float = 15.0) -> None:
        self.allow_private = allow_private
        self.connect_timeout = connect_timeout
        self.blocked: list[str] = []
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await self._serve(reader, writer)
        except (
            ConnectionError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
            OSError,
        ):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
            if task is not None:
                self._tasks.discard(task)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        if len(head) > MAX_HEAD:
            return
        request_line, _, rest = head.partition(b"\r\n")
        try:
            method, target, version = request_line.decode("latin-1").split(" ", 2)
        except ValueError:
            return
        if method.upper() == "CONNECT":
            host, _, port_text = target.rpartition(":")
            host = host.strip("[]")
            port = int(port_text) if port_text.isdigit() else 443
            forward = b""
        else:
            url = urlsplit(target)
            if url.scheme not in ("http", "ws") or not url.hostname:
                await self._refuse(writer, f"unsupported request target {target[:80]!r}")
                return
            host, port = url.hostname, url.port or 80
            path = url.path or "/"
            if url.query:
                path += "?" + url.query
            headers = []
            upgrade = False
            for line in rest.split(b"\r\n"):
                if not line:
                    continue
                name = line.split(b":", 1)[0].strip().lower()
                if name.startswith(b"proxy-"):
                    continue
                if name == b"upgrade":
                    upgrade = True
                headers.append(line)
            if not upgrade:  # one request per upstream connection keeps routing exact
                headers = [
                    h
                    for h in headers
                    if h.split(b":", 1)[0].strip().lower() not in (b"connection", b"keep-alive")
                ]
                headers.append(b"Connection: close")
            forward = (
                f"{method} {path} {version}\r\n".encode("latin-1")
                + b"\r\n".join(headers)
                + b"\r\n\r\n"
            )
        try:
            addresses = await web_mod.resolve_allowed(host, port, allow_private=self.allow_private)
        except web_mod.BlockedAddress as exc:
            self.blocked.append(f"{host}:{port} ({exc})")
            await self._refuse(writer, str(exc))
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(addresses[0], port), timeout=self.connect_timeout
            )
        except (OSError, TimeoutError):
            await self._refuse(writer, f"cannot connect to {host}:{port}", status=502)
            return
        try:
            if forward:
                up_writer.write(forward)
            else:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        finally:
            with contextlib.suppress(Exception):
                up_writer.close()

    async def _refuse(self, writer: asyncio.StreamWriter, reason: str, status: int = 403) -> None:
        body = f"Blocked by Decapod: {reason}".encode()
        writer.write(
            f"HTTP/1.1 {status} Blocked\r\nContent-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        with contextlib.suppress(Exception):
            await writer.drain()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()


#: Chromium flags that force all traffic through the proxy.
def chromium_proxy_args(port: int) -> list[str]:
    return [
        f"--proxy-server=http://127.0.0.1:{port}",
        "--proxy-bypass-list=<-loopback>",  # do not let localhost skip the proxy
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--no-pings",
        "--dns-prefetch-disable",
    ]
