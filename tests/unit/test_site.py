import json

import httpx

from decapod.config import Secrets
from decapod.domain import ActionSpec
from decapod.publishing import build_site, parse_note
from decapod.tools import ToolExecutor, ToolRegistry, builtin_tools


def write_note(folder, name, title, body, tags="[kelp]"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(
        f"---\ntitle: {title!r}\ndate: {name[:10]}\nauthor: Testpod (autonomous AI agent)\n"
        f"tags: {tags}\n---\n\n# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def test_parse_and_build_site(tmp_path):
    drive = tmp_path / "drive"
    pub = drive / "published"
    write_note(
        pub,
        "2026-01-02-kelp.md",
        "Kelp's secrets",
        "Kelp grows **fast**.\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))",
    )
    write_note(pub, "2026-03-04-tides.md", "Tides & moons", "- one\n- two", tags="[]")
    (pub / "index.md").write_text("# index\n")
    note = parse_note(pub / "2026-01-02-kelp.md")
    assert note.title == "Kelp's secrets" and note.tags == ["kelp"]
    assert not note.body_md.startswith("#")  # duplicate H1 dropped
    result = build_site(
        drive,
        title="Testpod",
        description="Research <notes>",
        author="Testpod",
        base_url="https://me.github.io/",
    )
    assert [n.slug for n in result.notes] == ["2026-03-04-tides", "2026-01-02-kelp"]
    assert set(result.files) >= {
        "index.html",
        "feed.xml",
        "style.css",
        "posts/2026-01-02-kelp.html",
    }
    page = (drive / "site/posts/2026-01-02-kelp.html").read_text(encoding="utf-8")
    assert "<strong>fast</strong>" in page and "<script>" not in page
    assert 'href="javascript' not in page
    index = (drive / "site/index.html").read_text(encoding="utf-8")
    assert index.index("Tides &amp; moons") < index.index("Kelp&#x27;s secrets")
    assert "Research &lt;notes&gt;" in index
    feed = (drive / "site/feed.xml").read_text(encoding="utf-8")
    assert "https://me.github.io/posts/2026-01-02-kelp.html" in feed
    import xml.etree.ElementTree as ET

    assert len(ET.fromstring(feed).findall("./channel/item")) == 2
    build_site(drive, title="T", description="d", author="a")  # rebuild replaces atomically
    assert not (drive / ".site-build").exists() and not (drive / ".site-old").exists()


async def test_publish_rebuilds_site(make_ctx, settings):
    registry = ToolRegistry(settings, Secrets({}))
    for tool in builtin_tools():
        registry.register(tool)
    ctx = make_ctx()
    res = await ToolExecutor(registry).execute(
        ActionSpec(
            tool="notes.publish", args={"title": "First light", "content": "Our very first note."}
        ),
        ctx,
    )
    assert res.ok and "website rebuilt" in res.output
    assert (ctx.paths.drive / "site" / "index.html").exists()
    assert "site.deploy" in registry.unavailable  # no repo configured


async def test_site_deploy_single_commit(make_ctx, settings, charter):
    settings.tools.github.enabled = True
    settings.tools.site.repo = "me/notes"
    settings.tools.site.path = "docs"
    calls = []

    def handler(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        path = request.url.path
        if path.endswith("/git/ref/heads/gh-pages"):
            return httpx.Response(200, json={"object": {"sha": "parent1"}})
        if path.endswith("/git/commits/parent1"):
            return httpx.Response(200, json={"tree": {"sha": "tree0"}})
        if path.endswith("/git/trees/tree0"):
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": "docs/posts/old-note.html", "type": "blob", "sha": "x"},
                        {"path": "docs/CNAME", "type": "blob", "sha": "y"},
                        {"path": "posts/outside-prefix.html", "type": "blob", "sha": "z"},
                    ]
                },
            )
        if path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": f"blob{len(calls)}"})
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "tree1"})
        if path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "commit1"})
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "?"})

    secrets = {"GITHUB_TOKEN": "ghp_token_value_123"}
    registry = ToolRegistry(settings, Secrets(secrets))
    for tool in builtin_tools():
        registry.register(tool)
    ctx = make_ctx(handler, secrets=secrets)
    action = ActionSpec(tool="site.deploy", args={})
    _, check = registry.check(action, ctx)
    assert "nothing to deploy" in check.args_error
    assert check.scopes == ["github:me/notes:write"]  # granted by the test charter
    write_note(ctx.paths.drive / "published", "2026-01-01-a.md", "A", "body")
    _, check = registry.check(action, ctx)
    assert check.args_error is None
    res = await ToolExecutor(registry).execute(action, ctx)
    assert res.ok, res.error
    tree = next(body for method, path, body in calls if path.endswith("/git/trees"))
    assert tree["base_tree"] == "tree0"
    assert {e["path"] for e in tree["tree"]} >= {"docs/index.html", "docs/feed.xml"}
    deleted = {e["path"] for e in tree["tree"] if e["sha"] is None}
    assert deleted == {"docs/posts/old-note.html"}  # stale page removed, CNAME kept
    assert "removed 1 stale" in res.output
    commit = next(
        body for method, path, body in calls if path.endswith("/git/commits") and method == "POST"
    )
    assert commit["parents"] == ["parent1"] and "autonomous AI agent" in commit["message"]
    assert calls[-1][0] == "PATCH"


async def test_site_deploy_refuses_secrets(make_ctx, settings):
    settings.tools.github.enabled = True
    settings.tools.site.repo = "me/notes"
    secrets = {"GITHUB_TOKEN": "ghp_token_value_123", "API_KEY": "sk-leaky-secret-1"}
    registry = ToolRegistry(settings, Secrets(secrets))
    for tool in builtin_tools():
        registry.register(tool)
    ctx = make_ctx(lambda r: httpx.Response(500), secrets=secrets)
    write_note(ctx.paths.drive / "published", "2026-01-01-a.md", "A", "key sk-leaky-secret-1")
    build_site(ctx.paths.drive, title="T", description="d", author="a")
    res = await ToolExecutor(registry).execute(ActionSpec(tool="site.deploy", args={}), ctx)
    assert not res.ok and "secret" in res.error


def test_secret_variants_cover_escaping():
    from decapod.tools.builtin.site import secret_variants

    variants = secret_variants('Pa55&word"<x>')
    assert "Pa55&amp;word&quot;&lt;x&gt;" in variants
    assert "Pa55&amp;amp;word&amp;quot;&amp;lt;x&amp;gt;" in variants


def test_filenames_cannot_inject_html(tmp_path):
    import sys

    drive = tmp_path / "drive"
    if sys.platform == "win32":  # Windows forbids " < > in filenames; still hostile here
        name, expected = "x' onmouseover=alert(1) & y.md", "x-onmouseover-alert-1-y"
    else:
        name, expected = 'x"><img src=x onerror=alert(1)>.md', "x-img-src-x-onerror-alert-1"
    write_note(drive / "published", name, "Evil", "body")
    result = build_site(drive, title="T", description="d", author="a")
    index = (drive / "site" / "index.html").read_text(encoding="utf-8")
    for needle in ("onerror=", "onmouseover=", "<img"):
        assert needle not in index  # only escaped text or slug text remains
    assert result.notes[0].slug == expected
    assert (drive / "site" / "posts" / f"{expected}.html").exists()


async def test_drive_tools_cannot_write_the_site(make_ctx, settings):
    registry = ToolRegistry(settings, Secrets({}))
    for tool in builtin_tools():
        registry.register(tool)
    ctx = make_ctx()
    res = await ToolExecutor(registry).execute(
        ActionSpec(
            tool="drive.write", args={"path": "site/x.html", "content": "<script>alert(1)</script>"}
        ),
        ctx,
    )
    assert not res.ok and "reserved" in res.error
