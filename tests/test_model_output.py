import json
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI
from tenacity import wait_none

from app.config import DeepSeekConfig, load_config
from app.jobs.service import SummaryService
from app.llm.deepseek import (
    DeepSeekClient,
    NonRetryableModelOutputError,
    RetryableModelOutputError,
    SummaryError,
    retryable,
)
from app.llm.schemas import SummaryData
from tests.conftest import event
from tests.test_deepseek import completion


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr("app.llm.deepseek.wait_exponential", lambda **kwargs: wait_none())


def mock_client(responses, config=None):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[min(len(requests) - 1, len(responses) - 1)])

    client = DeepSeekClient(config or DeepSeekConfig(), "")
    client.client = AsyncOpenAI(api_key="test-only", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return client, requests


@pytest.mark.parametrize("thinking", [None, False, True])
async def test_thinking_request_body(thinking):
    config = DeepSeekConfig() if thinking is None else DeepSeekConfig(thinking=thinking)
    client, requests = mock_client([completion(SummaryData.empty().model_dump_json())], config)
    try:
        await client.summarize("chat", AsyncMock())
        assert requests[0]["thinking"] == {"type": "enabled" if thinking else "disabled"}
        assert requests[0]["response_format"] == {"type": "json_object"}
    finally:
        await client.close()


def test_thinking_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    for value in ("true", "false"):
        path.write_text(f"deepseek:\n  thinking: {value}\n", encoding="utf-8")
        assert load_config(path).deepseek.thinking is (value == "true")


@pytest.mark.parametrize("first", ["", None, " \n\t", '{"topics": [', "{}",
                                   '{"topics":"bad"}',
                                   SummaryData.empty().model_dump_json()[:-1] + ',"action":"send_msg"}'])
async def test_bad_output_then_valid_succeeds(first):
    client, requests = mock_client([completion(first), completion(SummaryData.empty().model_dump_json())])
    on_retry = AsyncMock()
    try:
        assert (await client.summarize("chat", on_retry)).topics == []
        assert len(requests) == 2
        on_retry.assert_awaited_once()
    finally:
        await client.close()


@pytest.mark.parametrize("retries", [0, 2])
async def test_empty_output_exhausts_finite_budget(retries):
    client, requests = mock_client([completion("")], DeepSeekConfig(retries=retries))
    on_retry = AsyncMock()
    try:
        with pytest.raises(RetryableModelOutputError, match="空内容") as caught:
            await client.summarize("chat", on_retry)
        assert isinstance(caught.value, SummaryError)
        assert len(requests) == retries + 1
        assert on_retry.await_count == retries
    finally:
        await client.close()


@pytest.mark.parametrize("reason", ["length", "content_filter", "tool_calls", "unknown", None])
async def test_nonretryable_finish_reason(reason):
    response = completion("")  # Reason is checked BEFORE empty content.
    response["choices"][0]["finish_reason"] = reason
    client, requests = mock_client([response])
    on_retry = AsyncMock()
    try:
        with pytest.raises(NonRetryableModelOutputError) as caught:
            await client.summarize("chat", on_retry)
        if reason == "length":
            assert "缩短 summary 时间窗口" in str(caught.value)
        assert len(requests) == 1
        on_retry.assert_not_awaited()
    finally:
        await client.close()


@pytest.mark.parametrize("reason", ["insufficient_system_resource", "aborted"])
async def test_transient_finish_reason_then_success(reason):
    response = completion("")
    response["choices"][0]["finish_reason"] = reason
    client, requests = mock_client([response, completion(SummaryData.empty().model_dump_json())])
    on_retry = AsyncMock()
    try:
        assert (await client.summarize("chat", on_retry)).topics == []
        assert len(requests) == 2
        on_retry.assert_awaited_once()
    finally:
        await client.close()


def test_programming_value_error_is_not_retryable():
    assert not retryable(ValueError("programming error"))


async def test_output_retries_recorded_on_job(processor, repository, config):
    await processor.handle(event())
    await repository.queue_summaries(88, "cmd", [123], 0, 9999999999)
    client, requests = mock_client([completion(""), completion("bad json"),
                                    completion(SummaryData.empty().model_dump_json())])
    try:
        await SummaryService(repository, client, config).execute(await repository.claim_job())
        job = (await repository.query("SELECT * FROM summary_jobs"))[0]
        assert job["retry_count"] == 2
        assert job["status"] == "completed"
        assert len(requests) == 3
    finally:
        await client.close()
