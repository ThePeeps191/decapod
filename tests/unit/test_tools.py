import sys
from pathlib import Path

import httpx
import pytest

from decapod.config.settings import MCPServerSettings
from decapod.domain import ActionSpec, RiskTier, TaskStatus
from decapod.tools import MCPManager, ToolExecutor, ToolRegistry, builtin_tools
from decapod.tools.builtin import web as web_mod
from decapod.tools.builtin.drive import DriveError, resolve_in_drive, slugify
from decapod.tools.builtin.email import remember_contact
from decapod.tools.builtin.web import BlockedAddress, check_url, parse_duckduckgo, parse_feed
from decapod.tools.htmltext import html_to_text
from decapod.tools.sandbox import run_python


@pytest.fixture
def registry(settings):
    from decapod.config import Secrets

    reg = ToolRegistry(settings, Secrets({}))
    for tool in builtin_tools():
        reg.register(tool)
    return reg


async def run(registry, ctx, tool, **args):
    return await ToolExecutor(registry).execute(ActionSpec(tool=tool, args=args), ctx)


# --------------------------------------------------------------------------- registry


def test_registry_availability_and_catalog(registry, charter):
    from decapod.tools.sandbox import bwrap_works

    # code.run is only offered when bubblewrap can isolate it (isolation = "auto")
    assert ("code.run" in registry.unavailable) == (not bwrap_works())
    assert "email.send" in registry.unavailable
    catalog = registry.catalog(granted=lambda s: s in ("drive.read", "web.read"))
    assert "drive.read" in catalog and "drive.write(" not in catalog
    assert registry.risk_of(ActionSpec(tool="nope")) is RiskTier.IRREVERSIBLE
    assert registry.risk_of(ActionSpec.wait()) is RiskTier.INTERNAL
    with pytest.raises(ValueError):
        registry.register(builtin_tools()[0])


def test_registry_check_builds_policy_view(registry, make_ctx):
    ctx = make_ctx()
    tool, check = registry.check(ActionSpec(tool="drive.write", args={"path": "a.txt"}), ctx)
    assert check.args_error and "content" in check.args_error
    tool, check = registry.check(
        ActionSpec(tool="agenda.add_goal", args={"title": "Do it", "charter_refs": ["nope"]}), ctx
    )
    assert "unknown charter clause" in check.args_error
    assert registry.check(ActionSpec(tool="missing"), ctx) == (None, None)


# --------------------------------------------------------------------------- internal tools


async def test_agenda_tools(registry, make_ctx):
    ctx = make_ctx()
    res = await run(
        registry,
        ctx,
        "agenda.add_goal",
        title="Write a digest",
        charter_refs=["mission"],
        success_criteria=["published"],
    )
    assert res.ok and res.data["goal_id"] == 1
    res = await run(
        registry,
        ctx,
        "agenda.plan",
        goal_id=1,
        tasks=[{"title": "research"}, {"title": "write", "depends_on": [1]}],
    )
    assert res.ok and len(res.data["task_ids"]) == 2
    res = await run(
        registry, ctx, "agenda.plan", goal_id=1, tasks=[{"title": "xx", "depends_on": [1]}]
    )
    assert not res.ok and "invalid step" in res.error
    res = await run(registry, ctx, "agenda.update_task", task_id=1, status="done")
    assert res.ok and ctx.agenda.get_task(1).status is TaskStatus.DONE
    res = await run(registry, ctx, "agenda.update_goal", goal_id=1, priority=0.9)
    assert "0.90" in res.output
    status = await run(registry, ctx, "self.status")
    assert "Write a digest" in status.output and "Budget" in status.output
    assert (await run(registry, ctx, "clock.now")).output.startswith("UTC")


async def test_memory_tools_and_amendment(registry, make_ctx):
    ctx = make_ctx()
    assert (
        await run(registry, ctx, "memory.remember", content="Kelp grows fast", tags=["kelp"])
    ).ok
    found = await run(registry, ctx, "memory.search", query="kelp")
    assert "Kelp grows fast" in found.output
    res = await run(
        registry,
        ctx,
        "charter.propose_amendment",
        text="Allow email replies",
        rationale="Readers ask questions",
    )
    assert res.ok and ctx.db.scalar("SELECT COUNT(*) FROM amendments") == 1


# --------------------------------------------------------------------------- drive


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_resolve_in_drive(tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir()
    assert resolve_in_drive(drive, "a/b.txt") == (drive / "a/b.txt").resolve()
    for bad in ["../x", "/etc/passwd", "C:/x", "a/../../x", ".trash/x", "."]:
        with pytest.raises(DriveError):
            resolve_in_drive(drive, bad)
    (tmp_path / "outside").mkdir()
    (drive / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(DriveError):
        resolve_in_drive(drive, "link/secret")


async def test_drive_roundtrip(registry, make_ctx):
    ctx = make_ctx()
    assert (await run(registry, ctx, "drive.write", path="notes/a.md", content="hello")).ok
    assert (
        await run(registry, ctx, "drive.write", path="notes/a.md", content=" world", mode="append")
    ).ok
    read = await run(registry, ctx, "drive.read", path="notes/a.md")
    assert read.output == "hello world"
    partial = await run(registry, ctx, "drive.read", path="notes/a.md", max_chars=100, offset=6)
    assert partial.output == "world"
    listing = await run(registry, ctx, "drive.list", recursive=True)
    assert "notes/a.md" in listing.output
    assert (await run(registry, ctx, "drive.delete", path="notes/a.md")).ok
    assert not (ctx.paths.drive / "notes/a.md").exists()
    assert "notes/a.md" not in (await run(registry, ctx, "drive.list", recursive=True)).output
    assert not (await run(registry, ctx, "drive.read", path="../decapod.db")).ok


async def test_publish_notes(registry, make_ctx):
    ctx = make_ctx()
    first = await run(
        registry,
        ctx,
        "notes.publish",
        title="Tide Pools 101",
        content="All about tide pools.",
        tags=["ecology"],
    )
    second = await run(
        registry, ctx, "notes.publish", title="Tide Pools 101", content="A second edition."
    )
    assert first.data["path"] != second.data["path"]
    index = (await run(registry, ctx, "notes.index")).output
    assert index.count("Tide Pools 101") == 2
    body = (ctx.paths.drive / first.data["path"]).read_text()
    assert "autonomous AI agent" in body and "All about tide pools." in body
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("!!!") == "note"


# --------------------------------------------------------------------------- web


async def test_check_url_blocks_private_addresses():
    for url in [
        "http://127.0.0.1/",
        "http://10.0.0.5/x",
        "http://[::1]/",
        "http://100.100.100.200/latest/meta-data/",
        "http://169.254.169.254/",
        "http://[::ffff:127.0.0.1]/",
        "http://192.0.2.1/",
        "http://localhost/",
        "ftp://example.com/",
        "http:///nohost",
    ]:
        with pytest.raises(BlockedAddress):
            await check_url(url, allow_private=False)
    await check_url("http://127.0.0.1/", allow_private=True)


async def test_web_fetch_html(registry, make_ctx, monkeypatch):
    async def ok(url, *, allow_private):
        return None

    monkeypatch.setattr(web_mod, "check_url", ok)
    page = (
        "<html><head><title>Kelp</title><script>evil()</script></head><body>"
        "<nav>menu</nav><h1>Kelp forests</h1><p>Kelp is <b>fast</b>.</p>"
        "<a href='/more'>More</a></body></html>"
    )

    def handler(request):
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://example.com/page"})
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    ctx = make_ctx(handler)
    res = await run(
        registry, ctx, "web.fetch", url="https://example.com/redirect", include_links=True
    )
    assert res.ok, res.error
    assert "Title: Kelp" in res.output and "# Kelp forests" in res.output
    assert "evil" not in res.output and "menu" not in res.output
    assert "Untrusted content" in res.output
    assert "https://example.com/more" in res.output


async def test_web_fetch_redirect_to_private_is_blocked(registry, make_ctx):
    def handler(request):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    ctx = make_ctx(handler)
    # example.com may not resolve offline; either way the fetch must fail safely
    res = await run(registry, ctx, "web.fetch", url="http://93.184.215.14/start")
    assert not res.ok and ("non-public" in res.error or "fetch failed" in res.error)


async def test_web_search_duckduckgo(registry, make_ctx, monkeypatch):
    async def ok(url, *, allow_private):
        return None

    monkeypatch.setattr(web_mod, "check_url", ok)
    markup = (
        '<div><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fkelp.org%2F">'
        'Kelp <b>Org</b></a><a class="result__snippet">All about kelp</a></div>'
        '<div><a class="result__a" href="https://second.example/">Second</a></div>'
    )
    ctx = make_ctx(lambda r: httpx.Response(200, text=markup))
    res = await run(registry, ctx, "web.search", query="kelp")
    assert res.ok
    assert "https://kelp.org/" in res.output and "All about kelp" in res.output
    assert len(parse_duckduckgo(markup)) == 2


def test_parse_feed_rss_and_atom():
    rss = (
        "<rss><channel><item><title>A</title><link>https://a</link>"
        "<pubDate>Mon</pubDate><description>&lt;p&gt;Hi&lt;/p&gt;</description></item>"
        "</channel></rss>"
    )
    atom = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>B</title>'
        '<link href="https://b"/><updated>2026</updated><id>b1</id></entry></feed>'
    )
    assert parse_feed(rss)[0] == {
        "title": "A",
        "link": "https://a",
        "date": "Mon",
        "summary": "Hi",
        "id": "https://a",
    }
    assert parse_feed(atom)[0]["link"] == "https://b"
    import xml.etree.ElementTree as ET

    with pytest.raises(ET.ParseError):
        parse_feed('<!DOCTYPE x [<!ENTITY a "b">]><rss/>')


def test_html_to_text_edge_cases():
    title, text, links = html_to_text("<p>a<br>b</p><ul><li>x</li></ul><style>.c{}</style>")
    assert title == "" and "a\nb" in text and "- x" in text and ".c" not in text
    assert html_to_text("<div><unclosed")[1] == ""


# --------------------------------------------------------------------------- executor


async def test_executor_records_and_redacts(registry, make_ctx):
    ctx = make_ctx(secrets={"MY_TOKEN": "tok_supersecret_123"})
    await run(registry, ctx, "drive.write", path="s.txt", content="value tok_supersecret_123")
    res = await run(registry, ctx, "drive.read", path="s.txt")
    assert "[REDACTED]" in res.output and "supersecret" not in res.output
    assert ctx.db.scalar("SELECT COUNT(*) FROM tool_calls") == 2
    assert ctx.budget.usage("cycle", "c1").tool_calls == 2
    kinds = [e.kind for e in ctx.events.recent(10)]
    assert "tool.start" in kinds and "tool.result" in kinds


async def test_executor_idempotency(registry, make_ctx):
    ctx = make_ctx()
    executor = ToolExecutor(registry)
    action = ActionSpec(tool="drive.write", args={"path": "i.txt", "content": "x"})
    first = await executor.execute(action, ctx, idempotency_key="k1")
    again = await executor.execute(action, ctx, idempotency_key="k1")
    assert first.ok and again.data == {"replayed": True}
    ctx.db.execute("UPDATE tool_calls SET ok = NULL WHERE idempotency_key = 'k1'")
    interrupted = await executor.execute(action, ctx, idempotency_key="k1")
    assert not interrupted.ok and "interrupted" in interrupted.error


async def test_executor_handles_bad_input(registry, make_ctx):
    ctx = make_ctx()
    assert "unknown tool" in (await run(registry, ctx, "nope")).error
    assert "invalid arguments" in (await run(registry, ctx, "drive.read")).error


async def test_outbound_secret_guard(settings, make_ctx):
    from decapod.config import Secrets
    from decapod.tools.base import Tool, ToolArgs

    class PostArgs(ToolArgs):
        body: str

    class Post(Tool):
        name = "test.post"
        description = "post"
        Args = PostArgs
        tier = RiskTier.EXTERNAL_WRITE
        outbound = True

        async def run(self, args, ctx):
            raise AssertionError("must not run")

    reg = ToolRegistry(settings, Secrets({}))
    reg.register(Post())
    ctx = make_ctx(secrets={"API_KEY": "sk-live-abcdefgh"})
    res = await ToolExecutor(reg).execute(
        ActionSpec(tool="test.post", args={"body": "key sk-live-abcdefgh"}), ctx
    )
    assert not res.ok and "secret" in res.error


# --------------------------------------------------------------------------- sandbox


async def test_run_python_unisolated(tmp_path):
    result = await run_python(
        "print('hi'); import os; print(os.environ.get('MY_SECRET'))",
        workdir=tmp_path / "sb",
        instance_root=tmp_path,
        timeout_s=10,
        isolation="none",
    )
    assert result.exit_code == 0 and result.stdout.split() == ["hi", "None"]
    slow = await run_python(
        "import time; time.sleep(10)",
        workdir=tmp_path / "sb",
        instance_root=tmp_path,
        timeout_s=0.5,
        isolation="none",
    )
    assert slow.timed_out
    assert list((tmp_path / "sb").glob("run_*.py")) == []


async def test_code_tool_requires_isolation_opt_in(settings, make_ctx, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "x")
    from decapod.config import Secrets

    settings.tools.code.isolation = "none"
    reg = ToolRegistry(settings, Secrets({}))
    for tool in builtin_tools():
        reg.register(tool)
    ctx = make_ctx()
    ok = await run(reg, ctx, "code.run", code="print(2 + 2)")
    assert ok.ok and "4" in ok.output
    bad = await run(reg, ctx, "code.run", code="raise SystemExit(3)")
    assert not bad.ok and bad.data["exit_code"] == 3


# --------------------------------------------------------------------------- email precheck


def test_email_send_reply_only_policy(settings, make_ctx):
    from decapod.config import Secrets
    from decapod.tools.builtin.email import EmailSend

    settings.tools.email.enabled = True
    settings.tools.email.address = "bot@example.com"
    settings.tools.email.imap_host = settings.tools.email.smtp_host = "mail.example.com"
    secrets = Secrets({"DECAPOD_EMAIL_USERNAME": "u", "DECAPOD_EMAIL_PASSWORD": "p"})
    reg = ToolRegistry(settings, secrets)
    reg.register(EmailSend())
    ctx = make_ctx()
    action = ActionSpec(
        tool="email.send", args={"to": "Stranger <s@x.org>", "subject": "Hi", "body": "hello"}
    )
    _, check = reg.check(action, ctx)
    assert "reply_only" in check.args_error and check.outbound
    remember_contact(ctx, "S@x.org")
    _, check = reg.check(action, ctx)
    assert check.args_error is None


# --------------------------------------------------------------------------- MCP


async def test_mcp_roundtrip(make_ctx, settings):
    from decapod.config import Secrets

    script = Path(__file__).parent.parent / "fixtures" / "fake_mcp_server.py"
    manager = MCPManager(
        [
            MCPServerSettings(
                name="fake",
                command=[sys.executable, str(script)],
                tool_tiers={"echo": RiskTier.READ},
            )
        ]
    )
    tools = await manager.start()
    try:
        names = sorted(t.name for t in tools)
        assert names == ["mcp.fake.echo", "mcp.fake.fail"]
        echo = next(t for t in tools if t.name == "mcp.fake.echo")
        fail = next(t for t in tools if t.name == "mcp.fake.fail")
        assert echo.tier is RiskTier.READ and fail.tier is RiskTier.EXTERNAL_WRITE
        assert fail.outbound and "untrusted description" in echo.description
        reg = ToolRegistry(settings, Secrets({}))
        for tool in tools:
            reg.register(tool)
        ctx = make_ctx()
        _, check = reg.check(ActionSpec(tool="mcp.fake.echo", args={}), ctx)
        assert "missing required" in check.args_error and check.scopes == ["mcp:fake"]
        res = await ToolExecutor(reg).execute(
            ActionSpec(tool="mcp.fake.echo", args={"text": "hi"}), ctx
        )
        assert res.ok and "echo: hi" in res.output
        bad = await ToolExecutor(reg).execute(ActionSpec(tool="mcp.fake.fail", args={}), ctx)
        assert not bad.ok
    finally:
        await manager.close()


async def test_mcp_bad_command():
    manager = MCPManager([MCPServerSettings(name="ghost", command=["/nonexistent/binary"])])
    assert await manager.start() == []
    assert "ghost" in manager.errors


def test_plugin_tools(monkeypatch):
    from decapod.tools import plugins
    from decapod.tools.builtin.internal import ClockNow

    class Entry:
        def __init__(self, name, obj):
            self.name, self.obj = name, obj

        def load(self):
            if isinstance(self.obj, Exception):
                raise self.obj
            return self.obj

    entries = [
        Entry("cls", ClockNow),
        Entry("inst", ClockNow()),
        Entry("factory", lambda: [ClockNow(), "junk"]),
        Entry("bad", ImportError("x")),
    ]
    monkeypatch.setattr(plugins, "entry_points", lambda group: entries)
    errors = []
    tools = plugins.plugin_tools(lambda name, exc: errors.append(name))
    assert len(tools) == 3 and errors == ["bad"]


@pytest.mark.skipif(
    __import__("decapod.tools.sandbox", fromlist=["x"]).bwrap_path() is None,
    reason="bubblewrap not installed",
)
async def test_bwrap_sandbox_isolation(tmp_path):
    root = tmp_path / "inst"
    (root / "drive").mkdir(parents=True)
    (root / ".env").write_text("SECRET=topsecret\n")
    code = (
        "import os, socket, pathlib\n"
        "print('secret', os.environ.get('SECRET'))\n"
        "print('dotenv', pathlib.Path('../../.env').exists())\n"
        "try:\n    socket.create_connection(('1.1.1.1', 80), timeout=2); print('net open')\n"
        "except OSError:\n    print('net blocked')\n"
        "pathlib.Path('out.txt').write_text('hi')\n"
        "try:\n    pathlib.Path('/etc/decapod-test').write_text('x'); print('etc writable')\n"
        "except OSError:\n    print('etc blocked')\n"
        "try:\n    s = socket.socket(socket.AF_UNIX); s.connect(SOCK); print('socket open')\n"
        "except OSError:\n    print('socket blocked')\n"
    )
    import socket as socket_mod
    import tempfile

    sock_dir = tempfile.mkdtemp(dir="/var/tmp")
    sock_path = f"{sock_dir}/host.sock"
    server = socket_mod.socket(socket_mod.AF_UNIX)
    server.bind(sock_path)
    server.listen(1)
    code = f"SOCK = {sock_path!r}\n" + code
    result = await run_python(
        code, workdir=root / "drive" / "sandbox", instance_root=root, timeout_s=20, isolation="auto"
    )
    assert result.isolation == "bwrap" and result.exit_code == 0, result.stderr
    assert result.stdout.split("\n")[:4] == [
        "secret None",
        "dotenv False",
        "net blocked",
        "etc blocked",
    ]
    assert (root / "drive" / "sandbox" / "out.txt").read_text() == "hi"
