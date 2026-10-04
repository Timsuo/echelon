import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.delivery.models import DeliveryPreferences
from app.delivery.repository import DeliveryRepository
from app.delivery.worker import DeliveryWorker
from app.operations.diagnostics import outbox_stats, render_doctor, render_outbox
from tests.test_delivery import local, outbox, preferences
from tests.test_inbox_files import command
from tests.test_outbox_qos import enqueue


async def test_doctor_read_only_healthy_without_model(repository, config):
    await repository.db.health.start(dict(summary=120, attachment=120, configuration=120,
        history=120, triage=120, delivery=120, notifier=120))
    await repository.state('deepseek_health', 'Failed')
    before = repository.db.connection.total_changes
    text = await render_doctor(repository, SimpleNamespace(connected=True), config)
    assert repository.db.connection.total_changes == before
    for label in ('Service', 'OneBot', 'Database', 'Collection', 'Triage', 'History', 'Summary', 'Delivery', 'Outbox', 'DeepSeek', 'Attachments', 'Storage', 'Workers'):
        assert label in text
    assert '✅ quick_check' in text and 'STALE' not in text and 'Failed' in text
    assert 'Unavailable' not in text


@pytest.mark.parametrize('component', ['quick_check', 'triage_jobs', 'attachments', 'deepseek_health'])
async def test_doctor_independent_failure_privacy(component, repository, config, monkeypatch):
    original = repository.query
    async def fail(sql, args=()):
        if component in sql or component in args:
            raise sqlite3.OperationalError('DEEPSEEK_API_KEY=secret raw model response https://qq.com/private')
        return await original(sql, args)
    monkeypatch.setattr(repository, 'query', fail)
    text = await render_doctor(repository, SimpleNamespace(connected=False), config)
    assert 'unavailable' in text and 'Disconnected' in text and 'Outbox' in text
    assert 'secret' not in text and 'qq.com' not in text and 'API_KEY' not in text


async def test_doctor_quick_check_reports_failure_without_detail(repository, config, monkeypatch):
    original = repository.query
    async def corrupt(sql, args=()):
        return [{'quick_check': 'private payload corrupt'}] if sql == 'PRAGMA quick_check' else await original(sql, args)
    monkeypatch.setattr(repository, 'query', corrupt)
    text = await render_doctor(repository, SimpleNamespace(connected=True), config)
    assert 'quick_check failed' in text and 'private payload' not in text


async def test_doctor_stale_gap_failed_counts_and_queue_ages(repository, config):
    now = time.time()
    await repository.db.health.start({'triage': 60})
    await repository.query('UPDATE worker_health SET last_success=?', (now-600,))
    await repository.query("INSERT INTO collection_gaps(self_id,started_at,reason,created_at,updated_at) VALUES (88,?,'test',?,?)", (now-100, now-100, now))
    for table in ('summary_jobs', 'triage_jobs', 'history_sync_jobs'):
        extra_columns, extra_values = (',mode', ",'manual'") if table == 'history_sync_jobs' else ('', '')
        if table == 'triage_jobs':
            extra_columns, extra_values = ',not_before,source_kind', ",0,'realtime'"
        for status in ('queued', 'failed'):
            await repository.query(f'INSERT INTO {table}(self_id,group_id,window_start,window_end,status,created_at,completed_at{extra_columns}) VALUES (88,123,1,2,?,?,?{extra_values})', (status, now-1300, now if status == 'failed' else None))
    text = await render_doctor(repository, SimpleNamespace(connected=False), config)
    assert 'STALE' in text and 'Unresolved gaps 1 ⚠️' in text
    assert text.count('Failed 24h 1') == 3 and text.count('21m') >= 3
    assert 'Disconnected' in text


async def test_worker_heartbeats_throttled_and_disabled_not_stale(repository):
    await repository.db.health.start({'summary': 120})
    before = repository.db.connection.total_changes
    for _ in range(20):
        await repository.db.health.beat('summary')
    assert repository.db.connection.total_changes == before
    repository.db.health.last_write['summary'] -= 31
    await repository.db.health.beat('summary')
    assert repository.db.connection.total_changes == before+1
    await repository.db.health.stop()
    assert (await repository.query('SELECT expected FROM worker_health'))[0]['expected'] == 0


async def test_outbox_diagnostics_counts_and_no_text(repository, config):
    first = await enqueue(repository, 'multi', text='private secret'*400)
    await repository.notification_result(first, 'ConnectionError')
    await enqueue(repository, 'ready')
    dead = await enqueue(repository, 'dead', text='raw model output')
    for _ in range(3):
        await repository.notification_result(dead, 'ActionRejectedError')
    stats = await outbox_stats(repository, 88)
    assert stats['ready'] == 1 and stats['retrying'] == 1 and stats['deferred'] >= 1 and stats['dead'] == 1
    text = await render_outbox(repository, 88, config.timezone, True)
    assert 'ActionRejectedError' in text and 'attempts=3' in text
    assert 'private secret' not in text and 'raw model' not in text
    await repository.query("INSERT INTO private_outbox(self_id,text,created_at,dead_letter_at,error) VALUES (222,'secret',1,2,'foreign_secret')")
    assert 'foreign_secret' not in await render_outbox(repository, 88, config.timezone, True)


@pytest.mark.parametrize('text', ['/doctor', '/outbox', '/outbox failed'])
async def test_diagnostic_commands_admin_and_account_boundaries(text, repository, processor):
    await processor.router.dispatch(command(text, user_id=100))
    assert not await repository.query('SELECT * FROM private_outbox')
    await processor.router.dispatch(command(text, self_id=222))
    assert '不匹配' in (await repository.query('SELECT text FROM private_outbox'))[0]['text']
    await processor.router.dispatch(command(text))
    rows = await repository.query('SELECT * FROM private_outbox WHERE self_id=88')
    assert rows and all(row['self_id'] == 88 for row in rows)


async def test_delivery_idle_does_not_scan_each_second(repository, config, monkeypatch):
    await preferences(repository, digest_enabled=False)
    delivery = DeliveryRepository(repository, config)
    tick = AsyncMock(wraps=delivery.tick)
    monkeypatch.setattr(delivery, 'tick', tick)
    clock = SimpleNamespace(value=time.time())
    monkeypatch.setattr('app.delivery.worker.time', SimpleNamespace(time=lambda: clock.value))
    waits = []
    async def advance(coroutine, timeout):
        coroutine.close()
        waits.append(timeout)
        if len(waits) == 4:
            raise asyncio.CancelledError
        clock.value += timeout
        raise TimeoutError
    monkeypatch.setattr('app.delivery.worker.asyncio.wait_for', advance)
    with pytest.raises(asyncio.CancelledError):
        await DeliveryWorker(delivery).run()
    assert tick.await_count == 2
    assert all(delay >= 29 for delay in waits)


@pytest.mark.parametrize('lost_event', [False, True])
async def test_manual_digest_wakeup_and_restart_db_recovery(lost_event, repository, config):
    await preferences(repository, send_empty_digest=True, digest_enabled=False)
    delivery = DeliveryRepository(repository, config)
    first_tick = asyncio.Event()
    original = delivery.tick
    async def tick(sid):
        result = await original(sid)
        first_tick.set()
        return result
    delivery.tick = tick
    if lost_event:
        await delivery.manual(88, 'm')
        await repository.db.close()
        await repository.db.open()
        repository.db.delivery_wakeup.clear()
    task = asyncio.create_task(DeliveryWorker(delivery).run())
    try:
        await asyncio.wait_for(first_tick.wait(), 3)
        if not lost_event:
            await delivery.manual(88, 'm')
        async def emitted():
            while not await outbox(repository, 'digest'):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(emitted(), 3)
        assert len(await outbox(repository, 'digest')) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_next_due_includes_schedule_and_quiet_end(repository, config, monkeypatch):
    now = local('2026-10-03T07:29:50')
    monkeypatch.setattr('app.delivery.repository.time', SimpleNamespace(time=lambda: now))
    delivery = DeliveryRepository(repository, config)
    assert await delivery.next_due(88, now+60) == now+10
    prefs = DeliveryPreferences(digest_times=['23:40'])
    await repository.query('INSERT INTO delivery_preferences VALUES (88,?,0)', (prefs.model_dump_json(),))
    now = local('2026-10-03T23:50:00')
    await repository.query("INSERT INTO digest_runs(self_id,kind,scheduled_for,window_start,window_end,created_at,status) VALUES (88,'scheduled',?,1,2,?,'deferred')", (now-600, now-600))
    assert await delivery.next_due(88, now+86400) == local('2026-10-04T07:00:00')


async def test_next_due_includes_deferred_alert(repository, config):
    now = time.time()
    await repository.query("INSERT INTO inbox_items(id,self_id,title,source_group_id,source_sender_id,event_time,created_at,updated_at) VALUES (1,88,'t',123,99,1,1,1)")
    await repository.query("INSERT INTO inbox_deliveries(self_id,inbox_item_id,kind,item_revision,fingerprint,snapshot_json,status,scheduled_for,created_at) VALUES (88,1,'urgent',1,'x','{}','deferred',?,?)", (now+5, now))
    assert await DeliveryRepository(repository, config).next_due(88, now+60) == now+5
