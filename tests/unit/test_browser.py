import glob
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from decapod.config import Secrets
from decapod.domain import ActionSpec
from decapod.tools import ToolExecutor, ToolRegistry
from decapod.tools.builtin.browser import BrowserRead, playwright_available

PAGE = b"""<!doctype html><html><head><title>JS page</title></head><body>
<div id="out">loading</div>
<script>document.getElementById('out').textContent = 'rendered by javascript';
fetch('http://10.255.255.1/secret').catch(() => {});</script>
</body></html>"""


def chromium_path():
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return found[-1] if found else None


def test_unavailable_without_playwright(settings, monkeypatch):
    from decapod.tools.builtin import browser

    monkeypatch.setattr(browser, "playwright_available", lambda: False)
    assert "decapod[browser]" in BrowserRead().unavailable_reason(settings, Secrets({}))
    settings.tools.browser.enabled = False
    assert "disabled" in BrowserRead().unavailable_reason(settings, Secrets({}))


async def test_browser_blocks_private_targets(settings, make_ctx):
    registry = ToolRegistry(settings, Secrets({}))
    registry.register(BrowserRead())
    if registry.get("browser.read") is None:
        pytest.skip("playwright not installed")
    res = await ToolExecutor(registry).execute(
        ActionSpec(tool="browser.read", args={"url": "http://127.0.0.1:9/"}), make_ctx()
    )
    assert not res.ok and "blocked" in res.error


@pytest.mark.skipif(not playwright_available(), reason="playwright not installed")
async def test_browser_renders_javascript(settings, make_ctx, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    from decapod.tools.builtin import web

    real_allowed = web.address_allowed

    def allowed(host, port, address, *, allow_private):
        # The test server is on localhost: allow exactly it, keep blocking everything else.
        if address == "127.0.0.1" and port == server.server_address[1]:
            return True
        return real_allowed(host, port, address, allow_private=False)

    monkeypatch.setattr(web, "address_allowed", allowed)
    settings.tools.browser.executable_path = chromium_path()
    registry = ToolRegistry(settings, Secrets({}))
    registry.register(BrowserRead())
    ctx = make_ctx()
    try:
        res = await ToolExecutor(registry).execute(
            ActionSpec(
                tool="browser.read",
                args={"url": f"http://127.0.0.1:{port}/", "wait_ms": 300, "screenshot": True},
            ),
            ctx,
        )
    finally:
        server.shutdown()
    if not res.ok and "browser error" in (res.error or ""):
        pytest.skip(f"no usable Chromium here: {res.error}")
    assert res.ok, res.error
    assert "rendered by javascript" in res.output and "Title: JS page" in res.output
    assert "non-public addresses were blocked" in res.output
    assert (ctx.paths.drive / res.data["screenshot"]).exists()


@pytest.mark.skipif(not playwright_available(), reason="playwright not installed")
async def test_browser_blocks_redirects_and_websockets_to_private_hosts(
    settings, make_ctx, monkeypatch
):
    """Redirect hops and WebSockets must hit the same address policy (review finding)."""
    internal_hits = []

    class Internal(BaseHTTPRequestHandler):
        def do_GET(self):
            internal_hits.append((self.path, self.headers.get("Upgrade")))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>INTERNAL-SECRET</body></html>")

        def log_message(self, *args):
            pass

    internal = ThreadingHTTPServer(("127.0.0.1", 0), Internal)
    threading.Thread(target=internal.serve_forever, daemon=True).start()
    secret_port = internal.server_address[1]

    class Public(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redir":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{secret_port}/meta")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"<html><body>public<script>try {{ new WebSocket("
                f"'ws://127.0.0.1:{secret_port}/ws'); }} catch (e) {{}}</script>"
                f"</body></html>".encode()
            )

        def log_message(self, *args):
            pass

    public = ThreadingHTTPServer(("127.0.0.1", 0), Public)
    threading.Thread(target=public.serve_forever, daemon=True).start()
    public_port = public.server_address[1]
    from decapod.tools.builtin import web

    def allowed(host, port, address, *, allow_private):
        return port == public_port  # only the "public" server counts as public

    monkeypatch.setattr(web, "address_allowed", allowed)
    settings.tools.browser.executable_path = chromium_path()
    registry = ToolRegistry(settings, Secrets({}))
    registry.register(BrowserRead())
    executor = ToolExecutor(registry)
    try:
        redirect = await executor.execute(
            ActionSpec(tool="browser.read", args={"url": f"http://127.0.0.1:{public_port}/redir"}),
            make_ctx(),
        )
        page = await executor.execute(
            ActionSpec(
                tool="browser.read",
                args={"url": f"http://127.0.0.1:{public_port}/page", "wait_ms": 800},
            ),
            make_ctx(),
        )
    finally:
        public.shutdown()
        internal.shutdown()
    if not page.ok and "browser error" in (page.error or ""):
        pytest.skip(f"no usable Chromium here: {page.error}")
    assert "INTERNAL-SECRET" not in (redirect.output or "")
    assert not redirect.ok or "INTERNAL" not in redirect.output
    assert page.ok and "public" in page.output
    assert internal_hits == []
