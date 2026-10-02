import asyncio
import json
import time
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.application import create_app
from app.attachments.storage import AttachmentStorage
from app.commands.authorization import propose_authorization
from app.delivery.models import DeliveryPreferences
from app.delivery.repository import DeliveryRepository
from app.history.worker import HistoryWorker
from app.jobs.service import SummaryService
from app.llm.schemas import SummaryData
from app.storage.history_repository import HistoryRepository
from app.storage.policy_repository import PolicyRepository
from app.triage.models import TriageResult
from tests.conftest import event
from tests.test_delivery import delivered, local, outbox, preferences, revision
from tests.test_inbox_files import gateway
from tests.test_server import credentials
from tests.test_triage import candidate, item


def test_allow_verification_roundtrip_does_not_block_ws_receiver(tmp_path, config):
    with TestClient(create_app(config, credentials(), tmp_path)) as client:
        with client.websocket_connect('/onebot/v11/ws', headers={'Authorization': 'Bearer test-token', 'X-Self-ID': '88'}) as ws:
            ws.send_json(event(message_type='private', message_id=10, message='/allow add 456'))
            found = False
            for _ in range(6):
                action = ws.receive_json()
                data = None
                if action['action'] == 'get_group_list':
                    assert action['params'] == {'no_cache': True}
                    data = [{'group_id': 456, 'group_name': '测试课程群'}]
                elif action['action'] == 'get_group_info':
                    assert action['params']['group_id'] == 456
                    data = {'group_id': 456, 'group_name': '测试课程群'}
                else:
                    assert action['action'] == 'send_private_msg'
                    found = '/confirm 1' in action['params']['message'][0]['data']['text']
                ws.send_json({'echo': action['echo'], 'status': 'ok', 'retcode': 0, 'data': data})
                if found:
                    break
            assert found
            services = client.app.state.services
            assert not client.portal.call(services.repository.authorizations.is_active, 88, 456)
            ws.send_json(event(message_type='private', message_id=11, message='/confirm 1'))
            ack = ws.receive_json()
            assert '配置已更新' in ack['params']['message'][0]['data']['text']
            ws.send_json({'echo': ack['echo'], 'status': 'ok', 'retcode': 0})
            assert client.portal.call(services.repository.authorizations.is_active, 88, 456)
            ws.send_json(event(group_id=456, message_id=12))
            ws.send_json(event(message_type='private', message_id=13, message='/status'))
            status = ws.receive_json()
            ws.send_json({'echo': status['echo'], 'status': 'ok', 'retcode': 0})
            assert len(client.portal.call(services.repository.query, 'SELECT * FROM messages WHERE group_id=456')) == 1


@pytest.mark.parametrize('action,params', [('get_group_list', {}), ('get_group_info', {'group_id': 456})])
async def test_candidate_reads_allow_inactive_but_require_connected_account(action, params, repository, tmp_path):
    actions, socket = gateway(repository, AttachmentStorage(tmp_path/'attachments'))
    with pytest.raises(PermissionError):
        await actions.call(action, {'self_id': 222, **params})
    task = asyncio.create_task(actions.call(action, {'self_id': 88, **params}))
    payload = await asyncio.wait_for(socket.sent.get(), 2)
    actions.receive_response({'echo': payload['echo'], 'status': 'ok', 'retcode': 0, 'data': []})
    await task
    actions.detach(socket)
    with pytest.raises((PermissionError, ConnectionError)):
        await actions.call(action, {'self_id': 88, **params})


async def test_final_gateway_check_after_send_lock_wait(repository, tmp_path):
    actions, socket = gateway(repository, AttachmentStorage(tmp_path/'attachments'))
    checked = asyncio.Event()
    original = repository.authorizations.is_active
    async def check(sid, gid):
        result = await original(sid, gid)
        checked.set()
        return result
    repository.authorizations.is_active = check
    await actions._send_lock.acquire()
    task = asyncio.create_task(actions.call('get_group_file_url', {'self_id': 88, 'group_id': 123, 'file_id': 'f'}))
    await asyncio.wait_for(checked.wait(), 2)
    await repository.authorizations.deactivate(88, 123)
    actions._send_lock.release()
    with pytest.raises(PermissionError):
        await task
    assert socket.sent.empty()


async def test_queued_history_does_not_invoke_adapter_after_remove(repository, processor, config):
    history = HistoryRepository(repository, config)
    await history.manual(88, 123)
    job = await history.claim(88)
    await repository.authorizations.deactivate(88, 123)
    adapter = AsyncMock()
    await HistoryWorker(history, adapter, processor).execute(job)
    adapter.fetch.assert_not_awaited()
    assert (await repository.query('SELECT status FROM history_sync_jobs'))[0]['status'] == 'failed'


async def test_summary_remove_while_inference_running_cannot_commit(repository, processor, config):
    await processor.handle(event())
    await repository.queue_summaries(88, 's', [123], 0, time.time())
    async def summarize(*args):
        await repository.authorizations.deactivate(88, 123)
        return SummaryData.empty()
    llm = AsyncMock()
    llm.summarize.side_effect = summarize
    await SummaryService(repository, llm, config).execute(await repository.claim_job())
    assert not await repository.query('SELECT * FROM summaries')
    assert not await outbox(repository, 'summary')


async def test_authorization_confirm_atomic_with_status(repository, monkeypatch):
    policies = PolicyRepository(repository.db)
    proposal = await propose_authorization(policies, 88, 99, 123, 'remove')
    original = aiosqlite.Connection.execute
    def fail(connection, sql, *args, **kwargs):
        if 'UPDATE configuration_proposals SET status=?' in sql:
            raise RuntimeError('crash before confirmation commit')
        return original(connection, sql, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, 'execute', fail)
        with pytest.raises(RuntimeError):
            await policies.resolve(88, 99, proposal, True)
    assert await repository.authorizations.is_active(88, 123)
    assert (await repository.query('SELECT status FROM configuration_proposals'))[0]['status'] == 'pending'


@pytest.mark.parametrize('producer', ['delivery', 'digest'])
async def test_delivery_and_outbox_atomic_rollback(producer, repository, processor, config, monkeypatch):
    await preferences(repository, urgent_enabled=producer == 'delivery')
    await revision(repository, processor, config)
    delivery = DeliveryRepository(repository, config)
    if producer == 'digest':
        await delivery.manual(88, 'd')
    original = aiosqlite.Connection.execute
    def fail(connection, sql, *args, **kwargs):
        table = 'inbox_deliveries' if producer == 'delivery' else 'digest_runs'
        if f"UPDATE {table} SET status='enqueued'" in sql:
            raise RuntimeError('crash after outbox insert before status update')
        return original(connection, sql, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, 'execute', fail)
        with pytest.raises(RuntimeError):
            await delivery.tick(88)
    assert not await outbox(repository, producer)
    assert not await repository.query('SELECT * FROM digest_run_items')
    await delivery.tick(88)
    assert len(await outbox(repository, producer)) == 1
    await delivered(repository, producer)
    await repository.db.close()
    await repository.db.open()
    await delivery.tick(88)
    assert len(await outbox(repository, producer)) == 1


async def test_revision_event_rolls_back_with_triage_result(repository, processor, config, monkeypatch):
    triage, job, ids, candidates = await candidate(repository, config, processor)
    original = aiosqlite.Connection.execute
    def fail(connection, sql, *args, **kwargs):
        if 'UPDATE triage_jobs SET status=\'completed\'' in sql:
            raise RuntimeError('crash after revision event')
        return original(connection, sql, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, 'execute', fail)
        with pytest.raises(RuntimeError):
            await triage.apply(job, TriageResult(items=[item(ids)], ignored_message_ids=[]), candidates, False, 'mock')
    assert not await repository.query('SELECT * FROM inbox_revision_events')
    assert not await repository.query('SELECT * FROM inbox_items')


async def test_deferred_digest_slots_coalesce_when_quiet_ends(repository, config):
    prefs = DeliveryPreferences(digest_times=['23:40', '00:20', '06:30'], send_empty_digest=True)
    await repository.query('INSERT INTO delivery_preferences VALUES (88,?,0)', (prefs.model_dump_json(),))
    start = local('2026-10-01T23:35')
    await repository.query('UPDATE delivery_settings SET delivery_start_at=?,schedule_cursor=?', (start, start))
    delivery = DeliveryRepository(repository, config)
    for clock in ('2026-10-01T23:40', '2026-10-02T00:20', '2026-10-02T06:30'):
        await delivery.tick(88, local(clock))
    assert not await outbox(repository, 'digest')
    await delivery.tick(88, local('2026-10-02T07:00'))
    assert len(await outbox(repository, 'digest')) == 1


async def test_removed_group_excluded_at_digest_enqueue(repository, processor, config):
    await preferences(repository)
    await revision(repository, processor, config)
    delivery = DeliveryRepository(repository, config)
    await delivery.manual(88, 'd')
    await repository.authorizations.deactivate(88, 123)
    await delivery.tick(88)
    assert not await outbox(repository, 'digest')
    assert not await outbox(repository)


async def test_digest_priority_order_and_snapshot(repository, processor, config):
    await preferences(repository)
    for priority in ('low', 'normal', 'high', 'critical'):
        await revision(repository, processor, config, priority=priority)
    delivery = DeliveryRepository(repository, config)
    await delivery.manual(88, 'd')
    await delivery.tick(88)
    text = (await outbox(repository, 'digest'))[0]['text']
    assert text.index('CRITICAL') < text.index('HIGH') < text.index('NORMAL') < text.index('LOW')
    snapshots = await repository.query('SELECT * FROM digest_run_items')
    assert len(snapshots) == 4
    assert {json.loads(s['snapshot_json'])['priority'] for s in snapshots} == {'low', 'normal', 'high', 'critical'}


async def test_reauthorization_rejects_old_history_batch(repository, processor, config):
    history = HistoryRepository(repository, config)
    await history.manual(88, 123)
    job = await history.claim(88)
    async def fetch(*args):
        await repository.authorizations.deactivate(88, 123)
        await repository.authorizations.activate(88, 123)
        return type('Batch', (), {'received': 1, 'invalid': 0, 'messages': [event()]})()
    adapter = AsyncMock()
    adapter.fetch.side_effect = fetch
    await HistoryWorker(history, adapter, processor).execute(job)
    assert not await repository.query('SELECT * FROM messages')
    assert (await repository.query('SELECT status FROM history_sync_jobs'))[0]['status'] == 'failed'


async def test_reauthorization_between_event_check_and_commit(repository, processor, monkeypatch):
    original = repository.add_message
    async def interrupted(record, *args):
        await repository.authorizations.deactivate(88, 123)
        await repository.authorizations.activate(88, 123)
        return await original(record, *args)
    monkeypatch.setattr(repository, 'add_message', interrupted)
    assert not await processor.handle(event())
    assert not await repository.query('SELECT * FROM messages')
    monkeypatch.setattr(repository, 'add_message', original)
    assert await processor.handle(event(message_id=2))
