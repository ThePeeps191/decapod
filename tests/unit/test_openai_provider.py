import json

import httpx
import pytest

from decapod.domain import Tier
from decapod.llm import Message, OpenAICompatibleProvider, ProviderError, ProviderRequest


def completion(content, reasoning=None, usage=True):
    message = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    body = {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "m-served",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    return body


def make(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatibleProvider("https://llm.example/v1", "key-123", http_client=client)


REQ = ProviderRequest(
    model="m",
    tier=Tier.FLASH,
    messages=[Message.user("hi")],
    json_mode="object",
    json_schema={"type": "object"},
)


async def test_complete_parses_content_reasoning_and_usage():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=completion('{"a": 1}', reasoning="thinking..."))

    provider = make(handler)
    out = await provider.complete(REQ)
    assert out.content == '{"a": 1}' and out.reasoning == "thinking..."
    assert (out.tokens_in, out.tokens_out, out.model) == (11, 7, "m-served")
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["auth"] == "Bearer key-123"
    schema_req = REQ.model_copy(update={"json_mode": "schema", "schema_name": "Answer"})
    await provider.complete(schema_req)
    assert seen["body"]["response_format"]["type"] == "json_schema"
    await provider.close()


@pytest.mark.parametrize(
    ("status", "retryable", "bad_format"),
    [
        (400, False, True),
        (401, False, False),
        (404, False, False),
        (429, True, False),
        (500, True, False),
        (503, True, False),
    ],
)
async def test_error_mapping(status, retryable, bad_format):
    def handler(request):
        message = "response_format is not supported" if status == 400 else "nope"
        return httpx.Response(status, json={"error": {"message": message}})

    provider = make(handler)
    with pytest.raises(ProviderError) as info:
        await provider.complete(REQ)
    assert info.value.retryable is retryable
    assert info.value.bad_response_format is bad_format
    await provider.close()


async def test_connection_error_is_retryable():
    def handler(request):
        raise httpx.ConnectError("refused")

    provider = make(handler)
    with pytest.raises(ProviderError) as info:
        await provider.complete(REQ)
    assert info.value.retryable


async def test_missing_usage_and_embeddings():
    def handler(request):
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": "e",
                    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )
        return httpx.Response(200, json=completion("hello", usage=False))

    provider = make(handler)
    out = await provider.complete(REQ.model_copy(update={"json_mode": "none"}))
    assert out.tokens_in is None and out.content == "hello"
    assert await provider.embed("e", ["x"]) == [[0.1, 0.2]]
