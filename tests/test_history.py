import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.config import HistoryConfig
from app.history.worker import HistoryWorker, evaluate_coverage
from app.jobs.service import SummaryService
from app.llm.schemas import SummaryData
from app.onebot.actions import ActionGateway, ActionRejectedError
from app.onebot.history import (
    GroupHistoryQuery,
    HistoryAdapter,
    HistoryIdentityError,
    HistorySchemaError,
    parse_history,
)
from app.storage.history_repository import HistoryRepository
from tests.conftest import event
from tests.policy_helpers import set_policy
from tests.test_actions import FakeSocket
from tests.test_attachments import file_event


@pytest.fixture
def history(repository, config):
    return HistoryRepository(repository, config)


async def job(history, repository, mode="manual", start=1000, end=1100):
    async with repository.db.transaction() as connection:
        identifier = await history._enqueue(connection, 88, 123, mode, start, end, 1)
    return await history.claim(88) if identifier else None


def worker(history, processor, messages):
    actions = AsyncMock()
    actions.call.return_value = {"messages": messages}
    return HistoryWorker(history, HistoryAdapter(actions), processor), actions


async def test_history_action_firewall_and_wire(repository):
    actions = ActionGateway(99, repository=repository, allowed_groups=[123])
    socket = FakeSocket()
    socket.headers = {"x-self-id": "88"}
    actions.attach(socket)
    params = {"self_id": 88, "group_id": 123, "count": 100}
    for bad in (params | {"self_id": 222}, params | {"group_id": 999}):
        with pytest.raises(PermissionError):
            await actions.call("get_group_msg_history", bad)
    for forbidden in ("send_group_msg", "delete_msg", "upload_group_file", "trans_group_file", "set_group_ban"):
        with pytest.raises(PermissionError):
            await actions.call(forbidden, {})
    task = asyncio.create_task(actions.call("get_group_msg_history", params))
    sent = await asyncio.wait_for(socket.sent.get(), 2)
    assert sent["params"] == {"group_id": "123", "count": 100, "reverseOrder": False}
    actions.receive_response({"echo": sent["echo"], "status": "ok", "retcode": 0, "data": {"messages": []}})
    assert await task == {"messages": []}
    assert socket.sent.empty()


@pytest.mark.parametrize("count", [0, 501, "100", True])
def test_history_count_strict_bounds(count):
    with pytest.raises(ValidationError):
        GroupHistoryQuery(self_id=88, group_id=123, count=count)


async def test_realtime_history_idempotence_source_and_timestamps(history, processor, repository):
    await processor.handle(event(time=1000))
    last = await repository.state("last_realtime_received_at")
    task = await job(history, repository)
    service, _ = worker(history, processor, [event(time=1000), event(message_id=2, time=1050)])
    await service.execute(task)
    messages = await repository.query("SELECT * FROM messages ORDER BY id")
    assert len(messages) == 2
    assert messages[0]["ingest_source"] == "realtime"
    assert messages[1]["ingest_source"] == "history_recovery" and messages[1]["event_time"] == 1050
    assert await repository.state("last_realtime_received_at") == last
    assert float(await repository.state("last_message_event_time")) == 1050
    result = (await repository.query("SELECT * FROM history_sync_jobs"))[0]
    assert result["messages_received"] == 2 and result["messages_inserted"] == 1


async def test_periodic_source_window_filter_and_no_notifications(history, processor, repository):
    task = await job(history, repository, mode="periodic")
    service, _ = worker(history, processor, [event(time=1), event(message_id=2, time=1050), event(message_id=3, time=1500)])
    await service.execute(task)
    messages = await repository.query("SELECT * FROM messages")
    assert len(messages) == 1 and messages[0]["ingest_source"] == "history_poll"
    assert await repository.state("last_realtime_received_at") is None
    assert await repository.query("SELECT * FROM private_outbox") == []


async def test_history_never_replays_private_commands(history, processor, repository):
    task = await job(history, repository)
    service, _ = worker(history, processor, [event(message_type="private", message="/config 123 mode inbox", time=1050), event(message_id=2, time=1051)])
    await service.execute(task)
    assert len(await repository.query("SELECT * FROM messages")) == 1
    assert await repository.query("SELECT * FROM configuration_proposals") == []
    assert not await processor.handle(event(message_type="private", message="/summary 2h"), ingest_source="history_recovery")
    assert await repository.query("SELECT * FROM summary_jobs") == []


@pytest.mark.parametrize("bad", [None, {}, {"time": None}, {"time": "1000"}, {"sender": None},
                                  {"message": [{"type": "text", "data": None}]}, {"message_id": None}])
def test_invalid_item_isolated(bad):
    invalid = bad if bad is None or bad == {} else event(time=1050) | bad
    batch = parse_history({"messages": [invalid, event(time=1050)]}, GroupHistoryQuery(self_id=88, group_id=123, count=10), 1100)
    assert len(batch.messages) == 1 and batch.invalid == 1


@pytest.mark.parametrize("data", [None, [], {}, {"messages": "bad"}, {"messages": [None] * 501}])
def test_bad_envelope_rejected(data):
    with pytest.raises(HistorySchemaError):
        parse_history(data, GroupHistoryQuery(self_id=88, group_id=123, count=10), 1100)


@pytest.mark.parametrize("field", ["self_id", "group_id"])
def test_foreign_history_identity_rejected(field):
    with pytest.raises(HistoryIdentityError):
        parse_history({"messages": [event(time=1000) | {field: 999}]}, GroupHistoryQuery(self_id=88, group_id=123, count=10), 1100)


def test_missing_self_id_inherits_authenticated_action_context():
    message = event(time=1000)
    del message["self_id"]
    batch = parse_history({"messages": [message]}, GroupHistoryQuery(self_id=88, group_id=123, count=10), 1100)
    assert batch.messages[0]["self_id"] == 88


async def test_history_files_policy_dedup_and_unavailable(history, processor, repository):
    await set_policy(repository)
    service, _ = worker(history, processor, [file_event(time=1050), event(message_id=2, time=1051,
        message=[{"type": "file", "data": {"name": "no-reference.txt"}}])])
    await service.execute(await job(history, repository))
    await service.execute(await job(history, repository))
    assert len(await repository.query("SELECT * FROM messages")) == 2
    assert len(await repository.query("SELECT * FROM inbox_items")) == 2
    files = await repository.query("SELECT * FROM attachments ORDER BY id")
    assert files[0]["download_status"] == "pending"
    assert files[1]["download_status"] == "failed" and files[1]["error"] == "file_reference_unavailable"


async def test_history_duplicate_does_not_create_retroactive_inbox(history, processor, repository):
    await processor.handle(file_event(time=1050))
    await set_policy(repository)
    service, _ = worker(history, processor, [file_event(time=1050)])
    await service.execute(await job(history, repository))
    assert await repository.query("SELECT * FROM inbox_items") == []


@pytest.mark.parametrize("oldest,newest,invalid,expected", [(900, 1200, 0, "likely_covered"),
    (1050, 1200, 0, "partial"), (900, 1050, 0, "partial"), (None, None, 0, "unknown"), (900, 1200, 1, "partial")])
def test_coverage_conservative(oldest, newest, invalid, expected):
    assert evaluate_coverage(oldest, newest, 1000, 1100, invalid) == expected


@pytest.mark.parametrize("error,retry", [(TimeoutError(), True), (ConnectionError(), True),
    (ActionRejectedError(), True), (HistorySchemaError(), False), (HistoryIdentityError(), False), (PermissionError(), False)])
async def test_retry_bounded_and_terminal_failures(error, retry, history, processor, repository):
    service, actions = worker(history, processor, [])
    actions.call.side_effect = error
    task = await job(history, repository)
    for attempt in range(3 if retry else 1):
        if attempt:
            await repository.query("UPDATE history_sync_jobs SET next_attempt=0")
            task = await history.claim(88)
        await service.execute(task)
    result = (await repository.query("SELECT * FROM history_sync_jobs"))[0]
    assert result["status"] == "failed" and result["coverage"] == "failed"
    assert actions.call.await_count == (3 if retry else 1)
    assert await history.claim(88) is None


async def test_ignore_before_execution_never_calls_api(history, processor, repository):
    task = await job(history, repository)
    await set_policy(repository, "ignore")
    service, actions = worker(history, processor, [event(time=1000)])
    await service.execute(task)
    actions.call.assert_not_awaited()
    assert await repository.query("SELECT * FROM messages") == []


async def test_disconnect_reconnect_and_recovery_report(history, processor, repository):
    await history.startup(now=900)
    await history.connected(88, "first", now=950)
    assert await repository.query("SELECT * FROM collection_gaps") == []
    await history.disconnected(88, "first", now=1000)
    await history.disconnected(88, "first", now=1001)
    gap = (await repository.query("SELECT * FROM collection_gaps"))[0]
    assert gap["started_at"] == 1000 and gap["ended_at"] is None
    await history.connected(88, "second", now=1100)
    task = await history.claim(88)
    assert task["mode"] == "reconnect" and task["gap_id"] == gap["id"]
    service, _ = worker(history, processor, [event(time=900), event(message_id=2, time=1200)])
    await service.execute(task)
    gap = (await repository.query("SELECT * FROM collection_gaps"))[0]
    assert gap["ended_at"] == 1100 and gap["recovery_status"] == "likely_covered"
    await history.report_ready(88, now=1150)
    assert await repository.query("SELECT * FROM private_outbox") == []
    await history.report_ready(88, now=1161)
    await history.report_ready(88, now=1162)
    reports = await repository.query("SELECT * FROM private_outbox")
    assert len(reports) == 1 and reports[0]["self_id"] == 88
    assert "绝对完整性保证" in reports[0]["text"] and "LIKELY_COVERED" in reports[0]["text"]


async def test_gap_empty_result_unknown_and_flaps_coalesced(history, processor, repository):
    await history.connected(88, "one", now=900)
    await history.disconnected(88, "one", now=1000)
    await history.connected(88, "two", now=1010)
    await history.disconnected(88, "one", now=1011)  # stale session cannot disconnect the new one
    assert len(await repository.query("SELECT * FROM collection_gaps")) == 1
    await history.disconnected(88, "two", now=1020)
    await history.connected(88, "three", now=1030)
    service, _ = worker(history, processor, [])
    for _ in range(2):
        await service.execute(await history.claim(88))
    await history.report_ready(88, now=1091)
    reports = await repository.query("SELECT * FROM private_outbox")
    assert len(reports) == 1 and "合并缺口：2" in reports[0]["text"] and "UNKNOWN" in reports[0]["text"]


async def test_clean_stop_start_gap_and_running_recovery(history, repository):
    await history.startup(now=900)
    await history.connected(88, "first", now=950)
    await history.stop(now=1000)
    assert await repository.state("service_clean_shutdown") == "true"
    await history.startup(now=2000)
    assert await repository.state("service_clean_shutdown") == "false"
    await history.connected(88, "second", now=2010)
    task = await history.claim(88)
    assert task["window_start"] == 1000 and task["window_end"] == 2010
    assert (await repository.query("SELECT reason FROM collection_gaps"))[0]["reason"] == "service_stop"
    await history.startup(now=2020)
    assert (await repository.query("SELECT status FROM history_sync_jobs WHERE id=?", (task["id"],)))[0]["status"] == "queued"


async def test_unclean_restart_uses_last_checkpoint(history, repository):
    await repository.state("last_ws_connected", "900")
    await repository.state("last_collection_checkpoint", "1000")
    await history.startup(now=2000)
    gap = (await repository.query("SELECT * FROM collection_gaps"))[0]
    assert gap["started_at"] == 1000 and gap["reason"] == "offline_window_unclean"


async def test_new_handshake_before_old_disconnect_write_still_records_gap(history, repository):
    await history.connected(88, "first", now=900)
    await history.connected(88, "second", now=1000)
    await history.disconnected(88, "first", now=1001)
    gaps = await repository.query("SELECT * FROM collection_gaps")
    assert len(gaps) == 1 and gaps[0]["started_at"] == 900 and gaps[0]["ended_at"] == 1000
    assert await repository.state("onebot_connected") == "true"
    assert len(await repository.query("SELECT * FROM history_sync_jobs")) == 1


@pytest.mark.parametrize("mode,interval", [("priority", 120), ("inbox", 300), ("summary_only", 1800), ("ignore", None)])
async def test_periodic_intervals_and_ignore(mode, interval, history, repository):
    await set_policy(repository, mode)
    await history.connected(88, "one", now=1000)
    await history.schedule(88, now=1000 + (interval or 9999) - 1)
    assert await repository.query("SELECT * FROM history_sync_jobs") == []
    await history.schedule(88, now=1000 + (interval or 9999))
    jobs = await repository.query("SELECT * FROM history_sync_jobs")
    assert len(jobs) == (0 if interval is None else 1)


async def test_summary_gap_warning_is_python_and_account_group_scoped(history, processor, repository, config):
    await history.connected(88, "one", now=900)
    await history.disconnected(88, "one", now=1000)
    await history.connected(88, "two", now=1100)
    service, _ = worker(history, processor, [event(time=1050)])
    await service.execute(await history.claim(88))
    await repository.queue_summaries(88, "cmd", [123], 950, 1200)
    llm = AsyncMock()
    llm.summarize.return_value = SummaryData.empty()
    await SummaryService(repository, llm, config).execute(await repository.claim_job())
    text = (await repository.query("SELECT rendered_text FROM summaries"))[0]["rendered_text"]
    assert "数据覆盖警告" in text and "PARTIAL" in text
    assert "数据覆盖警告" not in llm.summarize.call_args.args[0]
    assert await history.unresolved(222, 123, 950, 1200) == []
    assert await history.unresolved(88, 123, 1101, 1300) == []
    await repository.queue_summaries(88, "cmd2", [123], 1200, 1300)
    await SummaryService(repository, llm, config).execute(await repository.claim_job())
    assert "数据覆盖警告" not in (await repository.query("SELECT rendered_text FROM summaries ORDER BY id DESC"))[0]["rendered_text"]


@pytest.mark.parametrize("params", [{"reconnect_count": 501}, {"periodic_count": 0}, {"retry_count": -1},
    {"priority_interval_seconds": 1}, {"inbox_interval_seconds": 0}, {"summary_only_interval_seconds": 999999}, {"debounce_seconds": 0}])
def test_history_config_limits(params):
    with pytest.raises(ValidationError):
        HistoryConfig(**params)
