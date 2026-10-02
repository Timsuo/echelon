import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from app.commands.authorization import propose_authorization
from app.jobs.service import SummaryService
from app.onebot.actions import READ_ONLY_ACTIONS
from app.onebot.groups import GroupVerifier
from app.policies.worker import ConfigurationWorker
from app.storage.history_repository import HistoryRepository
from app.storage.policy_repository import PolicyRepository
from app.storage.repository import Repository
from app.storage.triage_repository import TriageRepository
from app.triage.models import TriageResult
from app.triage.worker import TriageWorker
from tests.conftest import event
from tests.policy_helpers import set_policy
from tests.test_attachments import build_worker, file_event
from tests.test_inbox_files import command, downloaded, gateway
from tests.test_triage import candidate, item


async def test_bootstrap_once_remove_restart_and_new_seed_ignored(repository):
    assert await repository.authorizations.list_active(88) == [123]
    await repository.authorizations.deactivate(88, 123)
    await repository.db.close()
    await repository.db.open()
    replacement = Repository(repository.db, authorization_seed=[123, 456])
    assert await replacement.bind_onebot(88)
    assert await replacement.authorizations.list_active(88) == []
    assert len(await replacement.authorizations.list_known(88)) == 1
    assert not await replacement.bind_onebot(222)
    assert await replacement.authorizations.list_known(222) == []


async def test_empty_seed_is_still_bootstrapped(repository):
    await repository.query('DELETE FROM authorization_settings')
    await repository.query('DELETE FROM group_authorizations')
    empty = Repository(repository.db)
    assert await empty.bind_onebot(88)
    await repository.bind_onebot(88)
    assert await repository.authorizations.list_active(88) == []


async def test_allow_add_verifies_then_confirm_and_remove_preserves_policy(processor, repository):
    policies = PolicyRepository(repository.db)
    actions = AsyncMock()
    actions.call.side_effect = [[{'group_id': 456}], {'group_id': 456, 'group_name': '真实群名'}]
    await processor.router.dispatch(command('/allow add 456'))
    assert not await repository.authorizations.is_active(88, 456)
    request = await policies.claim()
    llm = AsyncMock()
    await ConfigurationWorker(policies, llm, actions).execute(request)
    llm.parse_config.assert_not_awaited()
    assert not await repository.authorizations.is_active(88, 456)
    await processor.router.dispatch(command('/confirm 1'))
    assert await repository.authorizations.is_active(88, 456)
    assert (await policies.get(88, 456)).mode == 'summary_only'
    await set_policy(repository, 'priority', group_id=456, alias='我的别名')
    await processor.router.dispatch(command('/allow remove 456'))
    assert await repository.authorizations.is_active(88, 456)
    await processor.router.dispatch(command('/confirm 2'))
    assert not await repository.authorizations.is_active(88, 456)
    await propose_authorization(policies, 88, 99, 456, 'add', '真实新群名')
    text = (await repository.query('SELECT text FROM private_outbox ORDER BY id DESC LIMIT 1'))[0]['text']
    assert '恢复现有 Policy：PRIORITY' in text and 'True' in text
    await processor.router.dispatch(command('/confirm 3'))
    assert (await policies.get(88, 456)).alias == '我的别名'
    await processor.router.dispatch(command('/allow'))
    assert '我的别名' in (await repository.query('SELECT text FROM private_outbox ORDER BY id DESC LIMIT 1'))[0]['text']


@pytest.mark.parametrize('response', [[], [{'group_id': 999}], None, {'group_id': 456}])
async def test_nonmember_cannot_be_authorized(response):
    actions = AsyncMock()
    actions.call.return_value = response
    with pytest.raises(ValueError):
        await GroupVerifier(actions).verify(88, 456)
    assert actions.call.await_count == 1


@pytest.mark.parametrize('text', ['/allow add 456', '/allow remove 123', '/notify 每天08:00收信', '/digest now'])
async def test_admin_and_cross_account_commands_guard(text, processor, repository):
    await processor.router.dispatch(command(text, user_id=777))
    await processor.router.dispatch(command(text, self_id=222))
    assert not await repository.query('SELECT * FROM configuration_proposals')
    assert not await repository.query('SELECT * FROM configuration_requests')
    assert not await repository.query('SELECT * FROM digest_runs')


async def test_events_revoke_and_readd_preserve_old_data(repository, processor):
    await set_policy(repository)
    assert not await processor.handle(file_event(group_id=456))
    assert await processor.handle(file_event())
    await repository.authorizations.deactivate(88, 123)
    assert not await processor.handle(event(message_id=2))
    assert len(await repository.query('SELECT * FROM messages')) == 1
    assert len(await repository.query('SELECT * FROM inbox_items')) == 1
    assert len(await repository.query('SELECT * FROM attachments')) == 1
    assert (await repository.query('SELECT status FROM triage_message_state'))[0]['status'] == 'failed'
    await repository.authorizations.activate(88, 123)
    assert await processor.handle(event(message_id=2))


async def test_downloaded_file_remains_accessible_after_removal(processor, repository, tmp_path):
    await set_policy(repository)
    storage, attachment = await downloaded(processor, repository, tmp_path)
    actions, socket = gateway(repository, storage)
    await repository.authorizations.deactivate(88, 123)
    task = asyncio.create_task(actions.call('upload_private_file', {'user_id': 99, 'self_id': 88, 'attachment_id': attachment['id']}))
    payload = await asyncio.wait_for(socket.sent.get(), 2)
    assert payload['action'] == 'upload_private_file'
    actions.receive_response({'status': 'ok', 'retcode': 0, 'echo': payload['echo']})
    await task


@pytest.mark.parametrize('action,extra', [('get_group_file_url', {'file_id': 'f'}), ('get_group_msg_history', {'count': 10})])
async def test_removed_group_remote_reads_blocked(action, extra, repository, tmp_path):
    from app.attachments.storage import AttachmentStorage
    actions, socket = gateway(repository, AttachmentStorage(tmp_path / 'attachments'))
    await repository.authorizations.deactivate(88, 123)
    with pytest.raises(PermissionError):
        await actions.call(action, {'self_id': 88, 'group_id': 123, **extra})
    assert socket.sent.empty()


async def test_history_schedule_and_sync_after_remove(repository, config):
    history = HistoryRepository(repository, config)
    await history.connected(88, 'session')
    jobs = await history.manual(88, 123)
    assert jobs
    await repository.authorizations.deactivate(88, 123)
    await history.schedule(88, time.time()+99999)
    await history.connected(88, 'new')
    with pytest.raises(ValueError, match='未授权'):
        await history.manual(88, 123)
    assert all(j['status'] == 'failed' for j in await repository.query('SELECT * FROM history_sync_jobs'))


@pytest.mark.parametrize('stage', ['pending', 'running'])
async def test_triage_revocation_never_applies(stage, repository, processor, config):
    if stage == 'pending':
        await set_policy(repository)
        await processor.handle(event())
        await repository.authorizations.deactivate(88, 123)
        assert await TriageRepository(repository, config).claim(time.time()+601) is None
    else:
        repo, job, ids, candidates = await candidate(repository, config, processor)
        await repository.authorizations.deactivate(88, 123)
        with pytest.raises((ValueError, PermissionError)):
            await repo.apply(job, TriageResult(items=[item(ids)], ignored_message_ids=[]), candidates, False, 'mock')
        llm = AsyncMock()
        await TriageWorker(repo, llm).execute(job)
        llm.triage.assert_not_awaited()
    assert not await repository.query('SELECT * FROM inbox_items')
    assert (await repository.query('SELECT status FROM triage_message_state'))[0]['status'] == 'failed'


async def test_attachment_pending_and_inflight_revoke(repository, processor, tmp_path):
    await set_policy(repository)
    await processor.handle(file_event())
    async def response(request):
        await repository.authorizations.deactivate(88, 123)
        return httpx.Response(200, content=b'abc')
    worker, client, storage = build_worker(repository, tmp_path, response)
    try:
        attachment = await worker.repository.claim_attachment(88)
        await worker.process(attachment)
        row = await worker.repository.attachment(88, attachment['id'])
        assert row['download_status'] == 'failed'
        assert row['local_path'] is None
        assert not list(storage.root.rglob('*.part'))
        await repository.authorizations.activate(88, 123)
        await processor.handle(event(message_id=2, message=[{'type': 'file', 'data': {'name': 'b.txt', 'file_id': 'b', 'size': 3}}]))
        await repository.authorizations.deactivate(88, 123)
        assert await worker.repository.claim_attachment(88) is None
    finally:
        await client.aclose()


async def test_summary_jobs_and_config_fail_closed(repository, processor, config):
    await processor.router.dispatch(command('/summary 2h'))
    job = await repository.claim_job()
    await repository.authorizations.deactivate(88, 123)
    llm = AsyncMock()
    await SummaryService(repository, llm, config).execute(job)
    llm.summarize.assert_not_awaited()
    await processor.router.dispatch(command('/summary 30m').model_copy(update={'message_id': 'new'}))
    await processor.router.dispatch(command('/config 123 mode priority'))
    assert len(await repository.query('SELECT * FROM summary_jobs')) == 1
    assert not await repository.query('SELECT * FROM configuration_proposals')
    assert not await PolicyRepository(repository.db).listing(88)


def test_firewall_read_capabilities_are_explicit():
    assert READ_ONLY_ACTIONS == {'get_group_file_url', 'get_group_msg_history', 'get_group_info', 'get_group_list'}
