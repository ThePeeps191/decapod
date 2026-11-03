"""OpenAI-compatible providers (Anthropic, OpenAI, OpenRouter, DeepSeek, Gemini, Ollama, …)."""

from __future__ import annotations

from typing import Any

from decapod.llm.types import ProviderError, ProviderRequest, ProviderResponse


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        timeout: float = 120.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key or "missing-api-key",
            timeout=timeout,
            max_retries=0,  # retries are handled by the LLM service
            default_headers=extra_headers or None,
        )

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        import openai

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens:
            kwargs["max_tokens"] = request.max_tokens
        if request.json_mode == "object":
            kwargs["response_format"] = {"type": "json_object"}
        elif request.json_mode == "schema" and request.json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": (request.schema_name or "output")[:64],
                    "schema": request.json_schema,
                },
            }
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.BadRequestError as exc:
            text = str(exc).lower()
            raise ProviderError(
                f"bad request: {exc}",
                retryable=False,
                status=400,
                bad_response_format="response_format" in text or "json" in text,
            ) from exc
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise ProviderError(
                f"authentication failed: {exc}",
                retryable=False,
                status=getattr(exc, "status_code", 401),
            ) from exc
        except openai.NotFoundError as exc:
            raise ProviderError(
                f"model or endpoint not found: {exc}", retryable=False, status=404
            ) from exc
        except openai.RateLimitError as exc:
            raise ProviderError(f"rate limited: {exc}", retryable=True, status=429) from exc
        except openai.APIStatusError as exc:
            status = exc.status_code
            raise ProviderError(
                f"provider error {status}: {exc}", retryable=status >= 500, status=status
            ) from exc
        except (openai.APIConnectionError, openai.APITimeoutError) as exc:
            raise ProviderError(f"connection error: {exc}", retryable=True) from exc

        if not response.choices:
            raise ProviderError("provider returned no choices", retryable=True)
        message = response.choices[0].message
        content = message.content or ""
        reasoning = _reasoning_of(message)
        usage = response.usage
        return ProviderResponse(
            content=content,
            reasoning=reasoning,
            tokens_in=getattr(usage, "prompt_tokens", None) if usage else None,
            tokens_out=getattr(usage, "completion_tokens", None) if usage else None,
            model=response.model,
        )

    async def embed(
        self, model: str, texts: list[str], provider: str = "default"
    ) -> list[list[float]]:
        import openai

        try:
            response = await self._client.embeddings.create(model=model, input=texts)
        except openai.OpenAIError as exc:
            raise ProviderError(f"embedding failed: {exc}", retryable=False) from exc
        return [list(item.embedding) for item in response.data]

    async def close(self) -> None:
        await self._client.close()


def _reasoning_of(message: Any) -> str:
    for attr in ("reasoning", "reasoning_content"):
        value = getattr(message, attr, None)
        if isinstance(value, str) and value:
            return value
    extra = getattr(message, "model_extra", None) or {}
    for key in ("reasoning", "reasoning_content"):
        value = extra.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class MultiProvider:
    """Routes each request to the endpoint of its provider (one client per provider)."""

    def __init__(self, configs: dict[str, Any], secrets: Any, *, timeout: float = 120.0) -> None:
        self._configs = configs
        self._secrets = secrets
        self._timeout = timeout
        self._clients: dict[str, OpenAICompatibleProvider] = {}

    def _client(self, name: str) -> OpenAICompatibleProvider:
        client = self._clients.get(name)
        if client is not None:
            return client
        config = self._configs.get(name)
        if config is None:
            raise ProviderError(f"unknown provider {name!r}", retryable=False, status=404)
        key = self._secrets.get(config.api_key_env) if config.api_key_env else None
        if config.requires_key and not key:
            raise ProviderError(
                f"provider {name!r} has no API key ({config.api_key_env} is not set)",
                retryable=False,
                status=401,
            )
        client = OpenAICompatibleProvider(
            config.base_url,
            key or "not-needed",
            timeout=self._timeout,
            extra_headers=config.extra_headers,
        )
        self._clients[name] = client
        return client

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return await self._client(request.provider).complete(request)

    async def embed(
        self, model: str, texts: list[str], provider: str = "default"
    ) -> list[list[float]]:
        return await self._client(provider).embed(model, texts)

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
