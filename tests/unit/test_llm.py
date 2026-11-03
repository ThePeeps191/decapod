import pytest
from pydantic import BaseModel

from decapod.config.settings import LLMSettings, PriceSettings
from decapod.domain import (
    BudgetExceeded,
    Cost,
    LLMError,
    ProviderUnavailable,
    StructuredOutputError,
    Tier,
)
from decapod.llm import (
    LLM,
    Message,
    ProviderError,
    ProviderResponse,
    ReplayProvider,
    ScriptedProvider,
    hash_embedding,
)
from decapod.storage import Database, EventLog


class Answer(BaseModel):
    value: int
    note: str = ""


async def no_sleep(_):
    return None


def make_llm(provider, **overrides):
    settings = LLMSettings(**overrides)
    return LLM(settings, provider, sleep=no_sleep)


MSGS = [Message.user("hi")]


async def test_chat_basic():
    llm = make_llm(ScriptedProvider({"t": "hello"}))
    out = await llm.chat(MSGS, tier=Tier.FLASH, purpose="t")
    assert out.content == "hello" and out.tier is Tier.FLASH
    assert out.cost.llm_requests == 1


async def test_structured_with_prose_and_repair():
    provider = ScriptedProvider({"s": ["not json at all", 'Sure: {"value": 7}']})
    llm = make_llm(provider)
    out = await llm.structured(MSGS, Answer, tier=Tier.FLASH, purpose="s")
    assert out.value == 7
    assert len(provider.calls) == 2
    assert "not valid" in provider.calls[1].messages[-1].content
    assert provider.calls[0].json_mode == "object"


async def test_structured_gives_up():
    llm = make_llm(ScriptedProvider({"s": '{"wrong": true}'}), repair_attempts=1)
    with pytest.raises(StructuredOutputError):
        await llm.structured(MSGS, Answer, tier=Tier.FLASH, purpose="s")


async def test_retry_then_success():
    provider = ScriptedProvider({"r": [ProviderError("503", retryable=True), "ok"]})
    out = await make_llm(provider).chat(MSGS, tier=Tier.FLASH, purpose="r")
    assert out.content == "ok" and out.attempts == 2


async def test_fallback_to_next_tier():
    def respond(request):
        if request.tier is Tier.MAX:
            raise ProviderError("down", retryable=False, status=500)
        return f"from {request.tier.value}"

    out = await make_llm(ScriptedProvider({"f": respond})).chat(MSGS, tier=Tier.MAX, purpose="f")
    assert out.content == "from PRO" and out.tier is Tier.PRO


async def test_auth_error_does_not_fall_back():
    provider = ScriptedProvider({"a": ProviderError("401", retryable=False, status=401)})
    with pytest.raises(LLMError, match="401"):
        await make_llm(provider).chat(MSGS, tier=Tier.PRO, purpose="a")
    assert len(provider.calls) == 1


async def test_bad_response_format_disables_json_mode():
    def respond(request):
        if request.json_mode != "none":
            raise ProviderError(
                "response_format unsupported", retryable=False, status=400, bad_response_format=True
            )
        return '{"value": 1}'

    llm = make_llm(ScriptedProvider({"j": respond}))
    out = await llm.structured(MSGS, Answer, tier=Tier.FLASH, purpose="j")
    assert out.value == 1


async def test_circuit_breaker_opens():
    t = [0.0]
    provider = ScriptedProvider({"x": ProviderError("down", retryable=False, status=500)})
    llm = LLM(LLMSettings(circuit_breaker_failures=2), provider, sleep=no_sleep, clock=lambda: t[0])
    for _ in range(2):
        with pytest.raises(LLMError):
            await llm.chat(MSGS, tier=Tier.FLASH, purpose="x")
    with pytest.raises(ProviderUnavailable):
        await llm.chat(MSGS, tier=Tier.FLASH, purpose="x")
    t[0] += 10_000  # cooldown elapsed -> half-open probe allowed
    provider.add("x", "recovered")
    assert (await llm.chat(MSGS, tier=Tier.FLASH, purpose="x")).content == "recovered"
    assert not llm.breaker.is_open


async def test_cost_and_recording(tmp_path):
    db = Database(tmp_path / "l.db")
    log = EventLog(db)

    class Gate:
        def __init__(self):
            self.recorded = []

        def authorize(self, estimate, cycle_id):
            assert estimate.llm_requests == 1

        def reserve(self, estimate, cycle_id):
            self.authorize(estimate, cycle_id)
            return 1

        def release(self, token):
            pass

        def record(self, kind, cost, cycle_id):
            self.recorded.append(cost)

    gate = Gate()
    provider = ScriptedProvider(
        {"c": ProviderResponse(content="x", tokens_in=1_000_000, tokens_out=500_000)}
    )
    settings = LLMSettings(pricing={"default:flash": PriceSettings(input=1, output=2)})
    llm = LLM(settings, provider, gate=gate, recorder=log, sleep=no_sleep)
    out = await llm.chat(MSGS, tier=Tier.FLASH, purpose="c", cycle_id="c1")
    assert out.cost.usd == pytest.approx(2.0)
    assert gate.recorded[0].usd == pytest.approx(2.0)
    assert db.scalar("SELECT COUNT(*) FROM llm_calls WHERE cycle_id='c1'") == 1
    assert log.recent(1)[0].kind == "llm.call"
    db.close()


async def test_budget_preauthorization_blocks_call():
    class Gate:
        def authorize(self, estimate, cycle_id):
            raise BudgetExceeded("daily", "llm_requests", 10, 10)

        def reserve(self, estimate, cycle_id):
            return self.authorize(estimate, cycle_id)

        def release(self, token):
            pass

        def record(self, kind, cost, cycle_id):
            raise AssertionError("must not record")

    provider = ScriptedProvider({"b": "x"})
    llm = LLM(LLMSettings(), provider, gate=Gate(), sleep=no_sleep)
    with pytest.raises(BudgetExceeded):
        await llm.chat(MSGS, tier=Tier.FLASH, purpose="b")
    assert provider.calls == []


async def test_replay_provider():
    replay = ReplayProvider([("a", "one"), ("a", "two"), ("b", "bee")])
    llm = make_llm(replay)
    assert (await llm.chat(MSGS, tier=Tier.FLASH, purpose="a")).content == "one"
    assert (await llm.chat(MSGS, tier=Tier.FLASH, purpose="b")).content == "bee"
    assert (await llm.chat(MSGS, tier=Tier.FLASH, purpose="a")).content == "two"
    with pytest.raises(LLMError):
        await llm.chat(MSGS, tier=Tier.FLASH, purpose="a")
    assert "a" in replay.misses


async def test_embeddings_optional():
    llm = make_llm(ScriptedProvider())
    assert await llm.embed(["x"]) is None
    llm2 = make_llm(ScriptedProvider(), embeddings_model="e")
    vecs = await llm2.embed(["hello world"])
    assert len(vecs[0]) == 64
    assert hash_embedding("a b") == hash_embedding("a b")


def test_cost_type():
    assert Cost().tokens == 0


async def test_llm_call_log_is_redacted(tmp_path):
    db = Database(tmp_path / "r.db")
    log = EventLog(db, redact=lambda s: s.replace("sekrit-value", "[REDACTED]"))
    provider = ScriptedProvider({"r": "the key is sekrit-value"})
    llm = LLM(LLMSettings(), provider, recorder=log, sleep=no_sleep)
    await llm.chat([Message.user("echo sekrit-value")], tier=Tier.FLASH, purpose="r")
    row = db.query_one("SELECT request, response FROM llm_calls")
    assert "sekrit" not in row["request"] and "sekrit" not in row["response"]
    db.close()


async def test_concurrent_calls_cannot_overshoot_budget(tmp_path):
    import asyncio

    from decapod.config.settings import BudgetLimits, BudgetSettings
    from decapod.control import Budget

    db = Database(tmp_path / "b.db")
    budget = Budget(
        db,
        BudgetSettings(
            cycle=BudgetLimits(),
            hourly=BudgetLimits(),
            daily=BudgetLimits(),
            lifetime=BudgetLimits(llm_requests=2),
        ),
    )

    provider = ScriptedProvider({"c": "ok"})
    original = provider.complete

    async def complete(request):
        await asyncio.sleep(0.01)
        return await original(request)

    provider.complete = complete
    llm = LLM(LLMSettings(fallbacks={}), provider, gate=budget, sleep=no_sleep)
    results = await asyncio.gather(
        *(llm.chat(MSGS, tier=Tier.FLASH, purpose="c") for _ in range(8)), return_exceptions=True
    )
    assert sum(not isinstance(r, Exception) for r in results) == 2
    assert all(isinstance(r, BudgetExceeded) for r in results if isinstance(r, Exception))
    assert budget.usage("lifetime").llm_requests == 2
    db.close()
