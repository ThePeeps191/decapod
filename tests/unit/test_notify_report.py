import json

import httpx

from decapod.config import Secrets
from decapod.config.settings import NotifySettings
from decapod.domain import Event
from decapod.notify import Message, Notifier, describe, expand, request_for


def test_request_formats():
    msg = Message("Pod: hi", "body", "x", "high")
    url, kw = request_for("ntfy:my-topic", msg)
    assert url == "https://ntfy.sh/my-topic" and kw["params"]["priority"] == "high"
    _, kw = request_for("ntfy:t", Message("Krabbé: approval needed ✓", "b", "x"))
    assert kw["params"]["title"] == "Krabbé: approval needed ✓"
    url, kw = request_for("https://hooks.slack.com/services/T/B/X", msg)
    assert kw["json"]["text"].startswith("*Pod: hi*")
    url, kw = request_for("https://discord.com/api/webhooks/1/abc", msg)
    assert "**Pod: hi**" in kw["json"]["content"]
    url, kw = request_for("https://example.org/hook", msg)
    assert kw["json"] == {"title": "Pod: hi", "message": "body", "kind": "x", "priority": "high"}
    assert expand("ntfy:${TOPIC}", Secrets({"TOPIC": "secret-topic"})) == "ntfy:secret-topic"


def test_describe_events():
    m = describe(
        Event(
            kind="approval.requested",
            summary="#3 email.send(...)",
            payload={"approval_id": 3, "reason": "tier 3"},
        ),
        "Pod",
    )
    assert m.priority == "high" and "approval" in m.title and "#3" in m.body
    m = describe(
        Event(kind="daemon.stop", payload={"state": "killed", "reason": "operator"}), "Pod"
    )
    assert m.title == "Pod: killed" and m.priority == "high"


async def test_notifier_filters_throttles_and_sends():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.content))
        return httpx.Response(200 if "good" in str(request.url) else 500)

    t = [0.0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        notifier = Notifier(
            NotifySettings(targets=["https://good.example/x", "https://bad.example/y"]),
            Secrets({}),
            http,
            "Pod",
            clock=lambda: t[0],
        )
        notifier.handle(Event(kind="cycle.end", summary="ignored"))
        notifier.handle(Event(kind="cycle.error", summary="boom"))
        notifier.handle(Event(kind="cycle.error", summary="boom again"))  # throttled
        notifier.handle(Event(kind="approval.requested", summary="a", payload={}))
        notifier.handle(Event(kind="approval.requested", summary="b", payload={}))  # exempt
        await notifier.drain()
    assert len(seen) == 6  # 3 messages x 2 targets
    assert len(notifier.sent) == 3 and len(notifier.errors) == 3
    assert json.loads(seen[0][1])["message"] == "boom"


async def test_daemon_notifies_and_writes_reports(tmp_path, monkeypatch):
    from decapod.daemon import Daemon
    from decapod.evals.brain import scripted_brain
    from decapod.report import build_report
    from decapod.runtime import Instance
    from decapod.util.timeutil import now, to_iso
    from tests.helpers import CONFIG, make_config

    posted = []

    def handler(request):
        if "notify.example" in str(request.url):
            posted.append(json.loads(request.content))
            return httpx.Response(200)
        return httpx.Response(404)

    config = make_config(
        tmp_path / "i",
        config=CONFIG + "\n[notify]\ntargets = "
        '["https://notify.example/hook"]\nevents = ["daemon.stop", '
        '"report.created"]\n[report]\nevery = "1h"\n',
    )
    instance = Instance(
        config,
        provider=scripted_brain(),
        control_poll=0.02,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    instance.db.set_state("last_report_at", to_iso(now().replace(year=now().year - 1)))
    await Daemon(config, instance=instance, max_cycles=2).run()
    kinds = [p["kind"] for p in posted]
    assert "daemon.stop" in kinds and "report.created" in kinds
    reports = list((config.paths.drive / "reports").glob("*.md"))
    assert len(reports) == 1 and "progress report" in reports[0].read_text(encoding="utf-8")

    from decapod.storage import Database
    from decapod.util.timeutil import parse_duration

    db = Database(config.paths.db)
    report = build_report(db, config.charter, parse_duration("7d"))
    db.close()
    assert "**Cycles:** 2" in report.markdown and "Agenda now" in report.markdown
    assert "2 cycles" in report.summary


async def test_ntfy_with_unicode_title_is_delivered():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        notifier = Notifier(NotifySettings(targets=["ntfy:topic"]), Secrets({}), http, "Krabbé")
        errors = await notifier.send(Message("Krabbé: hi", "body ✓", "x"))
    assert errors == [] and seen and seen[0].params["title"] == "Krabbé: hi"
