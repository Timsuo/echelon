import json
from unittest.mock import AsyncMock

import pytest

from app.jobs.service import SummaryService
from app.llm.schemas import SummaryData
from tests.conftest import event


async def test_window_messages_isolated_by_self_id(processor, repository):
    await processor.handle(event(self_id=111, message="A"))
    await processor.handle(event(self_id=222, message="B"))
    rows = await repository.window_messages(111, 123, 0, 9999999999, 100)
    assert [(row["self_id"], row["normalized_text"]) for row in rows] == [(111, "A")]


async def test_job_and_summary_keep_original_account_after_switch(processor, repository, config):
    await repository.state("onebot_self_id", "111")
    await processor.handle(event(self_id=111, message="account A only"))
    await processor.handle(event(self_id=222, message="account B only"))
    await processor.handle(event(self_id=111, message_type="private", message_id=10, message="/summary 2h"))
    queued = (await repository.query("SELECT * FROM summary_jobs"))[0]
    assert queued["self_id"] == 111
    await repository.state("onebot_self_id", "222")
    job = await repository.claim_job()
    llm = AsyncMock()
    llm.summarize.return_value = SummaryData.empty()
    await SummaryService(repository, llm, config).execute(job)
    records = json.loads(llm.summarize.call_args.args[0])["untrusted_chat_records"]
    assert [row["text"] for row in records] == ["account A only"]
    summary = (await repository.query("SELECT * FROM summaries"))[0]
    assert summary["self_id"] == job["self_id"] == 111
    assert summary["source_message_count"] == 1
    assert (await repository.query("SELECT status FROM summary_jobs"))[0]["status"] == "completed"


async def test_queue_rejects_account_different_from_binding(repository):
    with pytest.raises(ValueError, match="绑定账号不一致"):
        await repository.queue_summaries(777, "cmd", [123], 0, 100)
    assert await repository.query("SELECT * FROM summary_jobs") == []
    assert await repository.query("SELECT * FROM command_receipts") == []


async def test_job_without_self_id_never_queries_messages(repository, config):
    await repository.queue_summaries(88, "cmd", [123], 0, 100)
    job = await repository.claim_job()
    job["self_id"] = None  # Simulates an unresolved legacy job passed to the worker.
    repository.window_messages = AsyncMock()
    llm = AsyncMock()
    await SummaryService(repository, llm, config).execute(job)
    repository.window_messages.assert_not_called()
    llm.summarize.assert_not_called()
    assert (await repository.query("SELECT status FROM summary_jobs"))[0]["status"] == "failed"
