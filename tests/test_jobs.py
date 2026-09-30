import asyncio
from unittest.mock import AsyncMock

from app.jobs.service import SummaryService
from app.llm.deepseek import SummaryError
from app.llm.schemas import SummaryData
from app.storage.db import Database
from app.storage.repository import Repository
from tests.conftest import event


async def queue(repository):
    return await repository.queue_summaries(88, "cmd", [123], 0, 9999999999)


async def test_atomic_claim_across_connections(repository):
    await queue(repository)
    other_db = Database(repository.db.path)
    await other_db.open()
    try:
        claims = await asyncio.gather(repository.claim_job(), Repository(other_db).claim_job())
        assert sum(job is not None for job in claims) == 1
    finally:
        await other_db.close()


async def test_failure_keeps_messages(processor, repository, config):
    await processor.handle(event())
    await queue(repository)
    job = await repository.claim_job()
    llm = AsyncMock()
    llm.summarize.side_effect = SummaryError("测试 API 故障")
    await SummaryService(repository, llm, config).execute(job)
    assert len(await repository.query("SELECT * FROM messages")) == 1
    rows = await repository.query("SELECT * FROM summary_jobs")
    assert rows[0]["status"] == "failed"
    assert await repository.state("deepseek_health") == "Failed"
    await processor.handle(event(message_id=2))
    assert len(await repository.query("SELECT * FROM messages")) == 2


async def test_complete_and_restart_recovery(processor, repository, config):
    await processor.handle(event())
    await queue(repository)
    await repository.claim_job()
    path = repository.db.path
    await repository.db.close()
    await repository.db.open()
    assert repository.db.path == path
    await repository.recover()
    job = await repository.claim_job()
    llm = AsyncMock()
    llm.summarize.return_value = SummaryData.empty()
    await SummaryService(repository, llm, config).execute(job)
    assert (await repository.query("SELECT status FROM summary_jobs"))[0]["status"] == "completed"
    assert (await repository.query("SELECT * FROM summaries"))[0]["source_message_count"] == 1
    assert len(await repository.query("SELECT * FROM private_outbox")) == 2
    assert await repository.state("last_successful_api_call") is not None


async def test_outbox_survives_restart(repository):
    await repository.notify("result")
    item = await repository.next_notification()
    await repository.notification_result(item, "ConnectionError")
    await repository.db.close()
    await repository.db.open()
    rows = await repository.query("SELECT * FROM private_outbox")
    assert rows[0]["sent_at"] is None
    assert rows[0]["attempts"] == 1
    await repository.notification_result(rows[0])
    assert await repository.next_notification() is None


async def test_empty_window_does_not_call_api(repository, config):
    await queue(repository)
    llm = AsyncMock()
    await SummaryService(repository, llm, config).execute(await repository.claim_job())
    llm.summarize.assert_not_called()
    assert "没有已采集" in (await repository.query("SELECT rendered_text FROM summaries"))[0]["rendered_text"]
