import pytest

aiohttp = pytest.importorskip("aiohttp")
pytest.importorskip("textual_serve")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from decapod.tui.web import COOKIE, allowed_hosts, make_guard  # noqa: E402


async def make_client(token="tok123", port=8000):
    app = web.Application(middlewares=[make_guard(token, "127.0.0.1", port)])

    async def ok(request):
        return web.Response(text="ok")

    app.router.add_get("/", ok)
    app.router.add_get("/ws", ok)
    app.router.add_get("/static/app.js", ok)
    client = TestClient(TestServer(app, port=port))
    await client.start_server()
    return client


def host(port=8000):
    return {"Host": f"127.0.0.1:{port}"}


async def test_dashboard_requires_token_origin_and_host(unused_tcp_port):
    port = unused_tcp_port
    client = await make_client(port=port)
    try:
        assert (await client.get("/", headers=host(port))).status == 403
        assert (await client.get("/?token=wrong", headers=host(port))).status == 403
        assert (await client.get("/static/app.js", headers=host(port))).status == 200
        assert (await client.get("/ws", headers=host(port))).status == 403  # no cookie
        index = await client.get("/?token=tok123", headers=host(port))
        assert index.status == 200 and COOKIE in index.cookies
        assert (await client.get("/ws", headers=host(port))).status == 200  # cookie now set
        evil = await client.get("/ws", headers={**host(port), "Origin": "https://evil.example"})
        assert evil.status == 403
        same = await client.get("/ws", headers={**host(port), "Origin": f"http://localhost:{port}"})
        assert same.status == 200
        rebound = await client.get("/?token=tok123", headers={"Host": f"evil.example:{port}"})
        assert rebound.status == 403
    finally:
        await client.close()


def test_allowed_hosts():
    assert allowed_hosts("127.0.0.1", 8000) == {"127.0.0.1:8000", "localhost:8000", "[::1]:8000"}
    assert allowed_hosts("example.lan", 9000) == {"example.lan:9000"}
