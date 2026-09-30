import pytest

from tests.conftest import event


async def test_whitelist_and_duplicate(processor, repository):
    await processor.handle(event(group_id=999))
    assert await repository.query("SELECT * FROM messages") == []
    await processor.handle(event())
    await processor.handle(event())
    rows = await repository.query("SELECT * FROM messages")
    assert len(rows) == 1
    assert rows[0]["nickname"] == "Card"
    assert rows[0]["normalized_text"] == "hello"


async def test_unauthorized_command(processor, repository, caplog):
    await processor.handle(event(message_type="private", user_id=444, message="/summary 2h"))
    assert await repository.query("SELECT * FROM summary_jobs") == []
    assert await repository.query("SELECT * FROM private_outbox") == []
    assert "unauthorized" in caplog.text


async def test_summary_command_dedup(processor, repository):
    command = event(message_type="private", message="/summary 2h")
    await processor.handle(command)
    await processor.handle(command)
    jobs = await repository.query("SELECT * FROM summary_jobs")
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["self_id"] == 88
    assert jobs[0]["window_end"] - jobs[0]["window_start"] == pytest.approx(7200)


async def test_unknown_event_and_status(processor, repository):
    await processor.handle({"post_type": "notice"})
    await processor.handle(event(message_type="private", message="/status"))
    rows = await repository.query("SELECT text FROM private_outbox")
    assert "Unknown" in rows[0]["text"]
    assert "0 queued" in rows[0]["text"]
