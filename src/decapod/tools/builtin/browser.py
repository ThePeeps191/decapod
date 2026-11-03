"""browser.read: render a page in headless Chromium (optional Playwright dependency).

All browser traffic is forced through a local filtering proxy (see browser_proxy): every
connection, including redirect hops, sub-resources, XHR/fetch and WebSockets, is resolved
once, checked against the same address policy as web.fetch and pinned to the vetted IP, so
a page cannot pivot the browser into the local network or rebind DNS. Downloads and service
workers are disabled.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from decapod.config import Secrets, Settings
from decapod.domain import RiskTier, SideEffect, ToolResult
from decapod.tools.base import Tool, ToolArgs, ToolContext
from decapod.tools.builtin import web as web_mod
from decapod.tools.builtin.browser_proxy import FilteringProxy, chromium_proxy_args
from decapod.util.text import truncate


def playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


class BrowseArgs(ToolArgs):
    url: str = Field(min_length=8, max_length=2000)
    wait_ms: int = Field(default=0, ge=0, le=15_000, description="extra time to let scripts render")
    screenshot: bool = Field(default=False, description="save a screenshot in drive/screenshots")
    max_chars: int = Field(default=12_000, ge=500, le=60_000)


class BrowserRead(Tool):
    name = "browser.read"
    description = (
        "Open a page in a real (headless) browser, run its JavaScript and return the "
        "rendered text; optionally save a screenshot. Slower than web.fetch: use it "
        "for pages that need JavaScript. Untrusted content."
    )
    Args = BrowseArgs
    tier = RiskTier.READ
    side_effect = SideEffect.EXTERNAL_READ
    required_scopes = ("web.read",)

    def unavailable_reason(self, settings: Settings, secrets: Secrets) -> str | None:
        if not settings.tools.browser.enabled:
            return "the browser is disabled ([tools.browser] enabled = false)"
        if not playwright_available():
            return (
                "install the browser extra: pip install 'decapod[browser]' && "
                "playwright install chromium"
            )
        return None

    async def run(self, args: BrowseArgs, ctx: ToolContext) -> ToolResult:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        cfg = ctx.settings.tools.browser
        allow_private = ctx.settings.tools.web.allow_private_addresses
        try:
            await web_mod.check_url(args.url, allow_private=allow_private)
        except web_mod.BlockedAddress as exc:
            return ToolResult.failure(f"blocked: {exc}")
        blocked: list[str] = []

        async def guard(route: Any) -> None:
            url = route.request.url
            if url.startswith(("data:", "blob:", "about:")):
                await route.continue_()
                return
            try:
                await web_mod.check_url(url, allow_private=allow_private)
            except web_mod.BlockedAddress as exc:
                blocked.append(f"{url[:120]} ({exc})")
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        timeout_ms = cfg.timeout.total_seconds() * 1000
        proxy = FilteringProxy(allow_private=allow_private)
        proxy_port = await proxy.start()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    executable_path=cfg.executable_path or None,
                    headless=True,
                    args=chromium_proxy_args(proxy_port),
                )
                try:
                    context = await browser.new_context(
                        user_agent=ctx.settings.tools.web.user_agent,
                        accept_downloads=False,
                        service_workers="block",
                        java_script_enabled=True,
                    )
                    await context.route("**/*", guard)
                    page = await context.new_page()
                    response = await page.goto(
                        args.url, wait_until="domcontentloaded", timeout=timeout_ms
                    )
                    if args.wait_ms:
                        await page.wait_for_timeout(args.wait_ms)
                    title = await page.title()
                    text = await page.inner_text("body", timeout=timeout_ms)
                    final_url = page.url
                    shot_rel = None
                    if args.screenshot:
                        folder = ctx.paths.drive / "screenshots"
                        folder.mkdir(parents=True, exist_ok=True)
                        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
                        slug = re.sub(r"[^a-z0-9]+", "-", final_url.lower())[8:60].strip("-")
                        path = folder / f"{stamp}-{slug or 'page'}.png"
                        await page.screenshot(path=str(path), full_page=False)
                        shot_rel = path.relative_to(ctx.paths.drive).as_posix()
                finally:
                    await browser.close()
        except PlaywrightError as exc:
            message = str(exc).splitlines()[0]
            if "Executable doesn't exist" in str(exc):
                message = (
                    "Chromium is not installed for Playwright: run `playwright install "
                    "chromium` or set [tools.browser] executable_path"
                )
            return ToolResult.failure(f"browser error: {message}")
        finally:
            await proxy.close()
        blocked += [b for b in proxy.blocked if b not in blocked]
        status = response.status if response is not None else None
        if status is not None and status >= 400:
            return ToolResult.failure(f"HTTP {status} for {final_url}")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        header = web_mod.UNTRUSTED_NOTE.format(url=final_url)
        if title:
            header += f"Title: {title}\n"
        output = header + "\n" + truncate(text, args.max_chars)
        if shot_rel:
            output += f"\n\n[screenshot saved to {shot_rel}]"
        if blocked:
            output += f"\n\n[{len(blocked)} request(s) to non-public addresses were blocked]"
        return ToolResult.success(
            output,
            {"url": final_url, "title": title, "screenshot": shot_rel, "blocked": blocked[:10]},
        )


BROWSER_TOOLS: list[type[Tool]] = [BrowserRead]
