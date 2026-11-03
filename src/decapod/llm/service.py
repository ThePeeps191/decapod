"""The LLM service: tiers, retries, fallbacks, circuit breaker, metering, structured output."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from decapod.config.providers import Route, Routing, resolve_routes
from decapod.config.settings import LLMSettings
from decapod.domain import (
    BudgetExceeded,
    Cost,
    LLMError,
    ProviderUnavailable,
    StructuredOutputError,
    Tier,
)
from decapod.llm.types import (
    Completion,
    LLMProvider,
    Message,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    SpendGate,
)
from decapod.util.text import estimate_tokens, extract_json, json_dumps, one_line
from decapod.util.timeutil import now, to_iso

T = TypeVar("T", bound=BaseModel)
log = logging.getLogger(__name__)
Sleep = Callable[[float], Awaitable[None]]


class CircuitBreaker:
    def __init__(self, threshold: int, cooldown_s: float, clock: Callable[[], float]) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        # after the cooldown the breaker is half-open and lets a probe through
        return self.clock() - self.opened_at < self.cooldown_s

    def remaining(self) -> float:
        if self.opened_at is None:
            return 0.0
        return max(0.0, self.cooldown_s - (self.clock() - self.opened_at))

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.clock()


class LLM:
    """Facade used by every agent. Never talk to a provider directly."""

    def __init__(
        self,
        settings: LLMSettings,
        provider: LLMProvider,
        *,
        gate: SpendGate | None = None,
        routing: Routing | None = None,
        recorder: Any | None = None,  # decapod.storage.EventLog
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.routing = routing or resolve_routes(settings, None, placeholder=True)
        self.gate = gate
        self.recorder = recorder
        self._sleep = sleep
        self._clock = clock
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._pace_lock = asyncio.Lock()
        self._last_request = 0.0
        self._json_mode = settings.json_mode
        self.breaker = CircuitBreaker(
            settings.circuit_breaker_failures,
            settings.circuit_breaker_cooldown.total_seconds(),
            clock,
        )

    # ------------------------------------------------------------------ public API

    async def chat(
        self,
        messages: list[Message],
        *,
        tier: Tier,
        purpose: str,
        cycle_id: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
        schema_name: str | None = None,
    ) -> Completion:
        if self.breaker.is_open:
            raise ProviderUnavailable(
                f"provider circuit open for another {self.breaker.remaining():.0f}s"
            )
        chain = [tier, *[t for t in self.settings.fallbacks.get(tier, []) if t != tier]]
        last_error: Exception | None = None
        for current in chain:
            if current not in self.routing.routes:
                continue
            try:
                completion = await self._call_tier(
                    current,
                    messages,
                    purpose,
                    cycle_id,
                    temperature,
                    max_tokens,
                    json_schema,
                    schema_name,
                )
            except ProviderError as exc:
                last_error = exc
                if exc.status in (401, 403):
                    self.breaker.failure()
                    raise LLMError(str(exc)) from exc
                log.warning("tier %s failed for %s: %s", current.value, purpose, exc)
                continue
            self.breaker.success()
            return completion
        if last_error is None:
            raise LLMError(f"no model is configured for tier {tier.value} (or its fallbacks)")
        self.breaker.failure()
        raise LLMError(f"all tiers failed for {purpose}: {last_error}")

    async def structured(
        self,
        messages: list[Message],
        output: type[T],
        *,
        tier: Tier,
        purpose: str,
        cycle_id: str | None = None,
        temperature: float = 0.4,
        max_tokens: int | None = None,
    ) -> T:
        """Ask for JSON matching ``output`` and validate it, repairing on failure."""
        schema = output.model_json_schema()
        instruction = (
            "Respond with exactly one JSON object and nothing else (no prose, no code fences). "
            "It must validate against this JSON Schema:\n" + json_dumps(schema)
        )
        convo = [*messages, Message.user(instruction)]
        attempts = 1 + self.settings.repair_attempts
        last_error = ""
        for attempt in range(attempts):
            completion = await self.chat(
                convo,
                tier=tier,
                purpose=purpose,
                cycle_id=cycle_id,
                temperature=temperature if attempt == 0 else min(temperature, 0.2),
                max_tokens=max_tokens,
                json_schema=schema,
                schema_name=output.__name__,
            )
            try:
                data = extract_json(completion.content)
                return output.model_validate(data)
            except (ValueError, ValidationError) as exc:
                last_error = _short_error(exc)
                log.info(
                    "structured output invalid for %s (attempt %d): %s",
                    purpose,
                    attempt + 1,
                    last_error,
                )
                convo = [
                    *convo,
                    Message.assistant(completion.content[:4000]),
                    Message.user(
                        "That reply was not valid. Error: "
                        + last_error
                        + "\nReply again with ONLY the corrected JSON object."
                    ),
                ]
        raise StructuredOutputError(
            f"{purpose}: no valid {output.__name__} after {attempts} attempts: {last_error}"
        )

    def route(self, tier: Tier) -> Route | None:
        return self.routing.routes.get(tier)

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        route = self.routing.embeddings
        if route is None or not texts:
            return None
        try:
            return await self.provider.embed(route.model, texts, provider=route.provider)
        except ProviderError as exc:
            log.warning("embedding failed, falling back to keyword search: %s", exc)
            return None

    async def close(self) -> None:
        await self.provider.close()

    # ------------------------------------------------------------------ internals

    async def _call_tier(
        self,
        tier: Tier,
        messages: list[Message],
        purpose: str,
        cycle_id: str | None,
        temperature: float,
        max_tokens: int | None,
        json_schema: dict[str, Any] | None,
        schema_name: str | None,
    ) -> Completion:
        route = self.routing.routes[tier]
        model = route.model
        prompt_tokens = sum(estimate_tokens(m.content) for m in messages)
        price = self.settings.price_for(route.provider, model)
        output_cap = max_tokens or 4096
        # Worst-case estimate, reserved before *every* attempt (retries included) so
        # concurrent and repeated calls cannot overshoot a budget between settlements.
        estimate = Cost(
            tokens_in=prompt_tokens,
            llm_requests=1,
            usd=(prompt_tokens * price.input + output_cap * price.output) / 1_000_000,
        )

        json_mode = self._json_mode if json_schema is not None else "none"
        attempts = 0
        delay = 1.0
        while True:
            attempts += 1
            request = ProviderRequest(
                model=model,
                provider=route.provider,
                tier=tier,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
                json_schema=json_schema,
                schema_name=schema_name,
                purpose=purpose,
                cycle_id=cycle_id,
            )
            token = self.gate.reserve(estimate, cycle_id) if self.gate is not None else None
            started = self._clock()
            try:
                async with self._semaphore:
                    await self._pace()
                    response = await self.provider.complete(request)
            except ProviderError as exc:
                latency = int((self._clock() - started) * 1000)
                self._record(request, None, exc, latency, Cost(llm_requests=1), cycle_id)
                if exc.bad_response_format and json_mode != "none":
                    log.info("provider rejected response_format; disabling JSON mode")
                    self._json_mode = "none"
                    json_mode = "none"
                    continue
                if not exc.retryable or attempts > self.settings.max_retries:
                    raise
                await self._sleep(delay + random.uniform(0, delay / 2))
                delay = min(delay * 2, 30.0)
                continue
            finally:
                if token is not None and self.gate is not None:
                    self.gate.release(token)
            latency = int((self._clock() - started) * 1000)
            cost = self._cost(model, request, response)
            self._record(request, response, None, latency, cost, cycle_id)
            if not response.content.strip() and attempts <= self.settings.max_retries:
                await self._sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            return Completion(
                content=response.content,
                reasoning=response.reasoning,
                tier=tier,
                model=response.model or model,
                purpose=purpose,
                cost=cost,
                latency_ms=latency,
                attempts=attempts,
            )

    async def _pace(self) -> None:
        interval = self.settings.min_request_interval.total_seconds()
        if interval <= 0:
            return
        async with self._pace_lock:
            wait = self._last_request + interval - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._last_request = self._clock()

    def _cost(self, model: str, request: ProviderRequest, response: ProviderResponse) -> Cost:
        tokens_in = response.tokens_in
        if tokens_in is None:
            tokens_in = sum(estimate_tokens(m.content) for m in request.messages)
        tokens_out = response.tokens_out
        if tokens_out is None:
            tokens_out = estimate_tokens(response.content) + estimate_tokens(response.reasoning)
        price = self.settings.price_for(request.provider, model)
        usd = (tokens_in * price.input + tokens_out * price.output) / 1_000_000
        return Cost(usd=usd, tokens_in=tokens_in, tokens_out=tokens_out, llm_requests=1)

    def _record(
        self,
        request: ProviderRequest,
        response: ProviderResponse | None,
        error: Exception | None,
        latency_ms: int,
        cost: Cost,
        cycle_id: str | None,
    ) -> None:
        if self.gate is not None:
            try:
                self.gate.record("llm", cost, cycle_id)
            except BudgetExceeded:  # settlement never raises past the call that already ran
                pass
        if self.recorder is None:
            return
        db = self.recorder.db
        db.insert(
            "INSERT INTO llm_calls(ts, cycle_id, purpose, tier, model, request, response, "
            "reasoning, tokens_in, tokens_out, usd, latency_ms, ok, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                to_iso(now()),
                cycle_id,
                request.purpose,
                request.tier.value,
                f"{request.provider}:{request.model}",
                self.recorder.redact(json_dumps([m.model_dump() for m in request.messages])),
                self.recorder.redact(response.content) if response else None,
                self.recorder.redact(response.reasoning) if response else None,
                cost.tokens_in,
                cost.tokens_out,
                cost.usd,
                latency_ms,
                0 if error else 1,
                str(error) if error else None,
            ),
        )
        summary = f"{request.purpose} via {request.model}: " + (
            f"error {one_line(str(error), 120)}" if error else f"{cost.tokens} tok, {latency_ms}ms"
        )
        self.recorder.append(
            "llm.call" if error is None else "llm.error",
            summary,
            {
                "purpose": request.purpose,
                "tier": request.tier.value,
                "provider": request.provider,
                "model": request.model,
                "tokens_in": cost.tokens_in,
                "tokens_out": cost.tokens_out,
                "usd": cost.usd,
                "latency_ms": latency_ms,
                "reasoning": (response.reasoning[:4000] if response and response.reasoning else ""),
                "error": str(error) if error else None,
            },
            actor="llm",
            cycle_id=cycle_id,
        )


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors()[:6]:
            loc = ".".join(str(p) for p in err["loc"]) or "(root)"
            parts.append(f"{loc}: {err['msg']}")
        return "; ".join(parts)
    return one_line(str(exc), 300)
