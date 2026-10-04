import asyncio
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from app.commands.authorization import propose_authorization, verify_request
from app.delivery.models import DeliveryPreferenceIntent
from app.delivery.preferences import DeliveryPreferenceRepository
from app.jobs.service import SummaryService
from app.llm.schemas import SummaryData
from app.storage.policy_repository import PolicyRepository
from app.storage.preference_repository import PreferenceRepository
from tests.policy_helpers import set_policy
from tests.test_inbox_files import command
from tests.test_policies import intent as policy_intent
from tests.test_preferences import preference


async def test_summary_failure_has_account_name_and_safe_reason(repository):
    await set_policy(repository, 'summary_only', alias='高等数学')
    await repository.queue_summaries(88, 's', [123], 0, 100)
    job = await repository.claim_job()
    await repository.fail_job(job, '消息数量超限，请缩短总结窗口')
    notice = (await repository.query('SELECT * FROM private_outbox ORDER BY id DESC'))[0]
    assert notice['self_id'] == 88 and '高等数学' in notice['text'] and f"#{job['id']}" in notice['text']
    assert '缩短总结窗口' in notice['text']
    await repository.queue_summaries(88, 's2', [123], 0, 100)
    await repository.fail_job(await repository.claim_job(), 'secret raw provider traceback')
    assert 'secret' not in (await repository.query('SELECT text FROM private_outbox ORDER BY id DESC'))[0]['text']


@pytest.mark.parametrize('account', [None, 0, -1, True, '88'])
async def test_new_outbox_rejects_missing_or_invalid_account(account, repository):
    with pytest.raises(ValueError):
        await repository.notify('test', account)
    assert not await repository.query('SELECT * FROM private_outbox')


async def test_legacy_null_account_readable_without_backfill(repository):
    await repository.query("INSERT INTO private_outbox(text,created_at) VALUES ('legacy',1)")
    await repository.db.close()
    await repository.db.open()
    item = await repository.next_notification()
    assert item['self_id'] is None and item['text'] == 'legacy'
    await repository.notification_result(item)
    assert not await repository.next_notification()


async def test_summary_missing_metadata_uses_persisted_account_not_binding(repository, config):
    await repository.queue_summaries(88, 'legacy-summary', [123], 0, 100)
    job = await repository.claim_job()
    job['self_id'] = None
    await repository.state('onebot_self_id', '777')
    llm = AsyncMock()
    await SummaryService(repository, llm, config).execute(job)
    assert (await repository.query('SELECT status,self_id FROM summary_jobs'))[0] == {'status': 'failed', 'self_id': 88}
    assert all(row['self_id'] == 88 for row in await repository.query('SELECT * FROM private_outbox'))
    llm.summarize.assert_not_called()


@pytest.mark.parametrize('groups', [[], [123]])
async def test_zero_eligible_summary_receipt_is_atomic(groups, repository):
    await repository.authorizations.deactivate(88, 123)
    for _ in range(2):
        assert await repository.queue_summaries(88, 'same', groups, 0, 10) == []
    notices = await repository.query('SELECT * FROM private_outbox')
    assert len(notices) == 1 and '没有符合条件' in notices[0]['text']
    assert '/allow' in notices[0]['text'] and '总结已排队，任务：' not in notices[0]['text']


@pytest.mark.parametrize('text', ['/summary 2h', '/digest now', '/config 123 mode inbox',
    '/config 123 以后把通知整理起来', '/pref 考试重要', '/notify 关闭自动投递',
    '/notify 晚饭后收信', '/allow add 456', '/allow remove 123', '/confirm 1', '/cancel 1'])
async def test_private_command_replay_has_no_side_effect(text, repository, processor):
    if text.startswith(('/confirm', '/cancel')):
        await propose_authorization(PolicyRepository(repository.db), 88, 99, 123, 'remove')
    event = command(text)
    await processor.router.dispatch(event)
    tables = ['summary_jobs', 'digest_runs', 'configuration_requests', 'configuration_proposals', 'private_outbox', 'command_receipts', 'group_authorizations']
    before = {table: await repository.query(f'SELECT * FROM {table}') for table in tables}
    await processor.router.dispatch(event)
    assert before == {table: await repository.query(f'SELECT * FROM {table}') for table in tables}
    assert all(row['self_id'] is not None for row in before['private_outbox'])


async def request_case(repository, kind):
    policies = PolicyRepository(repository.db)
    texts = {'group_policy': '123 作为课程收件箱', 'triage_preferences': '考试重要',
             'delivery_preferences': '关闭投递', 'group_authorization': 'add'}
    await repository.query('INSERT INTO configuration_requests(self_id,admin_qq,group_id,target_group_id,message_id,input_text,created_at,kind) VALUES (88,99,123,123,?,?,1,?)', (kind, texts[kind], kind))
    request = await policies.claim()
    async def apply(current=request):
        if kind == 'group_policy':
            return await policies.propose(88, 99, 123, policy_intent(mode='inbox'), current['id'])
        if kind == 'triage_preferences':
            return await PreferenceRepository(policies).propose(current, preference(important_keywords_add=['考试']))
        if kind == 'delivery_preferences':
            return await DeliveryPreferenceRepository(policies).propose(current, DeliveryPreferenceIntent(action='update_delivery_preferences', changes={'enabled': False}, reason='test'))
        return await propose_authorization(policies, 88, 99, 456, 'add', '测试群', request_id=current['id'])
    return policies, request, apply


@pytest.mark.parametrize('kind', ['group_policy', 'triage_preferences', 'delivery_preferences', 'group_authorization'])
async def test_all_async_proposals_atomic_and_restart_idempotent(kind, repository, monkeypatch):
    policies, request, apply = await request_case(repository, kind)
    original = aiosqlite.Connection.execute
    def crash(connection, sql, *args, **kwargs):
        if "UPDATE configuration_requests SET status='completed'" in sql:
            raise RuntimeError('simulated crash after proposal insert')
        return original(connection, sql, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, 'execute', crash)
        with pytest.raises(RuntimeError):
            await apply()
    assert not await repository.query('SELECT * FROM configuration_proposals')
    assert not await repository.query('SELECT * FROM private_outbox')
    assert (await repository.query('SELECT status FROM configuration_requests'))[0]['status'] == 'running'
    await repository.db.close()
    await repository.db.open()
    await policies.recover()
    assert (await policies.claim())['id'] == request['id']
    await asyncio.gather(apply(), apply())
    await apply()
    proposals = await repository.query('SELECT * FROM configuration_proposals')
    assert len(proposals) == 1 and proposals[0]['source_request_id'] == request['id']
    assert len(await repository.query('SELECT * FROM private_outbox')) == 1
    assert (await repository.query('SELECT status FROM configuration_requests'))[0]['status'] == 'completed'


async def test_authorization_real_verification_request_atomically_completes(repository, processor):
    await processor.router.dispatch(command('/allow add 456'))
    policies = PolicyRepository(repository.db)
    request = await policies.claim()
    actions = AsyncMock()
    actions.call.side_effect = [[{'group_id': 456}], {'group_id': 456, 'group_name': '测试群'}]*2
    await verify_request(policies, actions, request)
    await verify_request(policies, actions, request)
    assert len(await repository.query('SELECT * FROM configuration_proposals')) == 1
    assert len(await repository.query('SELECT * FROM private_outbox')) == 2


async def test_summary_production_does_not_call_legacy_completion(repository, config, monkeypatch):
    legacy = AsyncMock(side_effect=AssertionError('v1 not allowed'))
    monkeypatch.setattr(repository, 'complete_job', legacy)
    await repository.queue_summaries(88, 's', [123], 0, 100)
    await SummaryService(repository, AsyncMock(summarize=AsyncMock(return_value=SummaryData.empty())), config).execute(await repository.claim_job())
    legacy.assert_not_awaited()
    assert (await repository.query('SELECT schema_version FROM summaries'))[0]['schema_version'] == 2
