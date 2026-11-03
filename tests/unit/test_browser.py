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

    real_check = web.check_url

    async def check(url, *, allow_private):
        # The test server is on localhost: allow exactly it, keep blocking everything else.
        if url.startswith(f"http://127.0.0.1:{port}/"):
            return None
        return await real_check(url, allow_private=False)

    monkeypatch.setattr(web, "check_url", check)
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
