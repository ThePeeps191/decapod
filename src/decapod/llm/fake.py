"""Deterministic providers for tests, evals and replay."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from decapod.llm.types import ProviderError, ProviderRequest, ProviderResponse

Responder = Callable[[ProviderRequest], str | dict[str, Any] | list[Any] | ProviderResponse]
Script = str | dict[str, Any] | list[Any] | Responder | ProviderError


class ScriptedProvider:
    """Answers by ``purpose`` glob. Each rule is a fixed reply, a callable or a queue.

    >>> p = ScriptedProvider({"caucus.propose.*": {"proposals": []}})
    """

    def __init__(
        self,
        rules: dict[str, Script | list[Script]] | None = None,
        default: Script | None = None,
    ) -> None:
        self.rules: list[tuple[str, list[Script], bool]] = []
        self.default = default
        self.calls: list[ProviderRequest] = []
        for pattern, script in (rules or {}).items():
            self.add(pattern, script)

    def add(
        self, pattern: str, script: Script | list[Script], *, queue: bool | None = None
    ) -> None:
        """Add a rule. A list is treated as a queue of replies unless ``queue=False``."""
        is_queue = isinstance(script, list) if queue is None else queue
        items = list(script) if is_queue and isinstance(script, list) else [script]
        self.rules.insert(0, (pattern, items, is_queue))

    def _render(self, script: Script, request: ProviderRequest) -> ProviderResponse:
        if isinstance(script, ProviderError):
            raise script
        if callable(script) and not isinstance(script, str | dict | list):
            script = script(request)  # type: ignore[assignment]
        if isinstance(script, ProviderResponse):
            return script
        if isinstance(script, dict | list):
            content = json.dumps(script)
        else:
            content = str(script)
        return ProviderResponse(
            content=content, tokens_in=_tok(request), tokens_out=len(content) // 4
        )

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        for pattern, items, is_queue in self.rules:
            if fnmatch.fnmatchcase(request.purpose, pattern):
                if is_queue:
                    if not items:
                        continue
                    script = items.pop(0) if len(items) > 1 else items[0]
                else:
                    script = items[0]
                return self._render(script, request)
        if self.default is not None:
            return self._render(self.default, request)
        raise ProviderError(
            f"no scripted response for purpose {request.purpose!r}", retryable=False
        )

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        return [hash_embedding(t) for t in texts]

    async def close(self) -> None:
        return None

    def purposes(self) -> list[str]:
        return [c.purpose for c in self.calls]


class ReplayProvider:
    """Serves responses recorded in ``llm_calls``, matched by (purpose, occurrence)."""

    def __init__(self, recorded: list[tuple[str, str]]) -> None:
        self._by_purpose: dict[str, list[str]] = defaultdict(list)
        for purpose, response in recorded:
            self._by_purpose[purpose].append(response)
        self._cursor: dict[str, int] = defaultdict(int)
        self.misses: list[str] = []

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        responses = self._by_purpose.get(request.purpose, [])
        index = self._cursor[request.purpose]
        if index >= len(responses):
            self.misses.append(request.purpose)
            raise ProviderError(
                f"no recorded response for {request.purpose!r} #{index}", retryable=False
            )
        self._cursor[request.purpose] += 1
        return ProviderResponse(content=responses[index], tokens_in=0, tokens_out=0)

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        return [hash_embedding(t) for t in texts]

    async def close(self) -> None:
        return None


def _tok(request: ProviderRequest) -> int:
    return sum(len(m.content) for m in request.messages) // 4


def hash_embedding(text: str, dims: int = 64) -> list[float]:
    """Deterministic bag-of-words embedding used by fakes (and as a last-resort fallback)."""
    vec = [0.0] * dims
    for word in text.lower().split():
        digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % dims
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[idx] += sign
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]
