import httpx
import pytest
from openai import AsyncOpenAI

from app.config import DeepSeekConfig
from app.llm.deepseek import DeepSeekClient, SummaryError
from app.llm.schemas import SummaryData


def completion(content):
    return {"id": "test", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}]}


@pytest.mark.parametrize("mode", ["retry", "invalid", "unauthorized"])
async def test_api_failures_and_retry(mode, caplog):
    calls = 0
    retries = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if mode == "unauthorized":
            return httpx.Response(401, json={"error": {"message": "bad auth"}})
        if mode == "retry" and calls == 1:
            return httpx.Response(503, json={"error": {"message": "busy"}})
        content = "INVALID JSON" if mode == "invalid" else SummaryData.empty().model_dump_json()
        return httpx.Response(200, json=completion(content))

    async def on_retry():
        nonlocal retries
        retries += 1

    client = DeepSeekClient(DeepSeekConfig(retries=1), "")
    client.client = AsyncOpenAI(api_key="test-only", max_retries=0,
                                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        if mode == "retry":
            assert (await client.summarize("chat", on_retry)).topics == []
            assert calls == 2 and retries == 1
        else:
            with pytest.raises(SummaryError):
                await client.summarize("chat", on_retry)
            assert calls == (2 if mode == "invalid" else 1)
            assert retries == (1 if mode == "invalid" else 0)
        if mode == "invalid":
            assert "INVALID JSON" not in caplog.text
            assert "raw_response" not in caplog.text
            assert "schema=SummaryData response_length=12" in caplog.text
    finally:
        await client.close()
