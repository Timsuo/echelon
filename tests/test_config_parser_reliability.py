import asyncio
import json
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError
from tenacity import wait_none

from app.config import DeepSeekConfig
from app.llm.deepseek import DeepSeekClient, retryable
from app.policies.errors import ConfigErrorCode, ConfigParseError
from app.policies.models import ConfigFeedback, ConfigParseResult, GroupPolicy
from app.policies.parser import ConfigIntentParser
from app.policies.worker import ConfigurationWorker
from app.storage.policy_repository import PolicyRepository
from tests.policy_helpers import set_policy
from tests.test_deepseek import completion
from tests.test_inbox_files import command
from tests.test_model_output import mock_client
from tests.test_policies import intent


@pytest.fixture
def policies(repository):
    return PolicyRepository(repository.db)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr('app.llm.deepseek.wait_exponential', lambda **kwargs: wait_none())


@pytest.mark.parametrize('body,changes', [
    ('mode inbox', {'mode': 'inbox'}),
    ('只总结', {'mode': 'summary_only'}),
    ('只需要总结', {'mode': 'summary_only'}),
    ('只做摘要', {'mode': 'summary_only'}),
    ('加入收件箱', {'mode': 'inbox'}),
    ('放进收件箱', {'mode': 'inbox'}),
    ('不进收件箱', {'inbox_enabled': False}),
    ('不要放进收件箱', {'inbox_enabled': False}),
    ('开启总结', {'summary_enabled': True}),
    ('不要总结', {'summary_enabled': False}),
    ('关闭总结', {'summary_enabled': False}),
    ('重点关注', {'mode': 'priority'}),
    ('不要重点关注', {'priority_watch_enabled': False}),
    ('暂停处理', {'mode': 'ignore'}),
    ('忽略这个群', {'mode': 'ignore'}),
    ('恢复正常处理', {'mode': 'summary_only'}),
    ('自动下载附件', {'attachment_download_enabled': True}),
    ('不要下载附件', {'attachment_download_enabled': False}),
    ('不自动下载附件', {'attachment_download_enabled': False}),
])
async def test_local_phrases_only_propose(body, changes, processor, repository, policies):
    before = await set_policy(repository, 'priority' if changes.get('mode') != 'priority' else 'ignore')
    if changes == {'attachment_download_enabled': True} or changes == {'summary_enabled': True}:
        before = await set_policy(repository, 'ignore')
    result = ConfigIntentParser.local(body)
    assert result.changes.model_dump(exclude_unset=True) == changes
    await processor.router.dispatch(command('/config 123 ' + body))
    assert not await repository.query('SELECT * FROM configuration_requests')
    assert await policies.get(88, 123) == before
    proposal = (await repository.query('SELECT * FROM configuration_proposals'))[0]
    await processor.router.dispatch(command(f"/confirm {proposal['id']}"))
    assert (await policies.get(88, 123)).model_dump() == before.model_dump() | result.changes.expanded()


@pytest.mark.parametrize('body', ['开启消息功能', '每周一自动切换成 priority', '不要总结但是下载附件',
                                 '接下来14天重点关注', '超过100MB不要下载附件'])
def test_uncertain_and_compound_input_is_not_local(body):
    assert ConfigIntentParser.local(body) is None


@pytest.mark.parametrize('prefix,body', [
    ('123 ', '以后这个群的消息帮我多留意'),
    ('123 ', '以后只需要整理一下聊天内容'),
    ('高数群', '以后作为收件箱处理'),
    ('123 ', '123 课程，10月15日，14天，100MB，请整理'),
    ('高数群', '10月15日、14天、100MB、课程123以及高数群的内容'),
])
async def test_body_only_model_proposal_and_replay(prefix, body, processor, repository, policies):
    before = await set_policy(repository, 'summary_only', alias='高数群')
    event = command('/config ' + prefix + body)
    await processor.router.dispatch(event)
    await processor.router.dispatch(event)
    assert len(await repository.query('SELECT * FROM configuration_requests')) == 1
    assert len(await repository.query('SELECT * FROM private_outbox')) == 1
    request = await policies.claim()
    assert request['target_group_id'] == 123
    assert request['input_text'] == request['intent_text'] == body
    llm = AsyncMock(parse_config=AsyncMock(return_value=intent(mode='priority')))
    worker = ConfigurationWorker(policies, llm)
    await worker.execute(request)
    await worker.execute(request)
    await processor.router.dispatch(event)
    llm.parse_config.assert_awaited_once()
    assert llm.parse_config.call_args.args[0] == body
    assert await policies.get(88, 123) == before
    proposals = await repository.query('SELECT * FROM configuration_proposals')
    assert len(proposals) == 1
    assert len(await repository.query('SELECT * FROM private_outbox')) == 2
    await processor.router.dispatch(command(f"/confirm {proposals[0]['id']}"))
    assert (await policies.get(88, 123)).mode == 'priority'


def test_only_unique_target_prefix_removed():
    rows = [GroupPolicy(self_id=88, group_id=123, alias='高数群'),
            GroupPolicy(self_id=88, group_id=456, alias='课程群')]
    assert ConfigIntentParser.parse_target_and_body('高数群参考课程群配置', rows) == (123, '参考课程群配置')
    with pytest.raises(ConfigParseError) as caught:
        ConfigIntentParser.parse_target_and_body('请关注高数群', rows)
    assert caught.value.code == ConfigErrorCode.AMBIGUOUS_TARGET
    rows.append(GroupPolicy(self_id=88, group_id=789, alias='高数'))
    with pytest.raises(ConfigParseError):
        ConfigIntentParser.parse_target_and_body('高数群重点关注', rows)


@pytest.mark.parametrize('action,body,hint', [
    ('clarify', '开启消息功能', '只总结、加入收件箱'),
    ('unsupported', '每周一自动切换成 priority', '不支持定时切换'),
])
async def test_feedback_completed_without_proposal_and_recovered_once(action, body, hint, processor, repository, policies, caplog):
    before = await policies.get(88, 123)
    event = command('/config 123 ' + body)
    await processor.router.dispatch(event)
    await policies.claim()  # Interrupted while running, before any result is persisted.
    await repository.db.close()
    await repository.db.open()
    await policies.recover()
    request = await policies.claim()
    feedback = ConfigFeedback(action=action, message='private provider secret prompt /allow add 999')
    client, calls = mock_client([completion(feedback.model_dump_json())])
    try:
        worker = ConfigurationWorker(policies, client)
        await asyncio.gather(worker.execute(request), worker.execute(request))
        await policies.recover()
        assert await policies.claim() is None
        await worker.execute(request)
        await processor.router.dispatch(event)
        row = (await repository.query('SELECT * FROM configuration_requests'))[0]
        assert row['status'] == 'completed' and row['retry_count'] == 0 and row['error'] is None
        assert not await repository.query('SELECT * FROM configuration_proposals')
        assert await policies.get(88, 123) == before
        notices = await repository.query('SELECT * FROM private_outbox')
        assert len(notices) == 2 and hint in notices[-1]['text']
        assert all(notice['self_id'] == 88 for notice in notices)
        assert 'private provider' not in str(notices) + caplog.text
        assert len(calls) <= 2  # Concurrent parsing is harmless; business result commits once.
    finally:
        await client.close()


async def test_feedback_transaction_rollback_and_recovery(processor, repository, policies, monkeypatch):
    await processor.router.dispatch(command('/config 123 开启消息功能'))
    request = await policies.claim()
    feedback = ConfigFeedback(action='clarify', message='clarify')
    original = aiosqlite.Connection.execute

    def crash(connection, sql, *args, **kwargs):
        if "UPDATE configuration_requests SET status='completed'" in sql:
            raise RuntimeError('simulated crash')
        return original(connection, sql, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, 'execute', crash)
        with pytest.raises(RuntimeError):
            await policies.complete_feedback(request, feedback)
    assert len(await repository.query('SELECT * FROM private_outbox')) == 1
    assert (await repository.query('SELECT status FROM configuration_requests'))[0]['status'] == 'running'
    await repository.db.close()
    await repository.db.open()
    await policies.recover()
    request = await policies.claim()
    await policies.complete_feedback(request, feedback)
    await policies.complete_feedback(request, feedback)
    assert len(await repository.query('SELECT * FROM private_outbox')) == 2


@pytest.mark.parametrize('payload', [
    {'action': 'update_group_policy', 'changes': {'mode': 'unknown'}, 'reason': 'x'},
    {'action': 'update_group_policy', 'changes': {'sql': 'DROP TABLE messages'}, 'reason': 'x'},
    {'action': 'update_group_policy', 'changes': {'shell': 'whoami'}, 'reason': 'x'},
    {'action': 'update_group_policy', 'changes': {'group_id': 999}, 'reason': 'x'},
    {'action': 'update_group_policy', 'changes': {'path': '/tmp'}, 'reason': 'x'},
    {'action': 'update_group_policy', 'changes': {'action': 'send_group_msg'}, 'reason': 'x'},
    {'action': 'update_group_policy', 'changes': {'mode': 'inbox'}, 'reason': 'x', 'group_id': 999},
    {'action': 'update_group_policy', 'changes': {'summary_enabled': 'true'}, 'reason': 'x'},
    {'action': 'update_group_policy', 'changes': {'summary_enabled': None}, 'reason': 'x'},
    {'action': 'update_group_policy', 'changes': {}, 'reason': 'x'},
    {'action': 'update_group_policy', 'changes': {'alias': 'a' * 65}, 'reason': 'x'},
    {'action': 'update_group_policy', 'changes': {'mode': 'inbox'}, 'reason': 'x' * 301},
    {'action': 'clarify', 'message': None},
    {'action': 'clarify', 'message': 'x' * 301},
    {'action': 'clarify', 'message': 'x', 'changes': {'mode': 'priority'}},
    {'action': 'unsupported', 'message': 'x', 'group_id': 999},
    {'action': 'shell', 'message': 'whoami'},
])
async def test_union_strict_schema_no_side_effect(payload, processor, repository, policies):
    with pytest.raises(ValidationError):
        ConfigParseResult.model_validate(payload)
    await processor.router.dispatch(command('/config 123 请留意这里的信息'))
    client, calls = mock_client([completion(json.dumps(payload))], DeepSeekConfig(retries=0))
    try:
        await ConfigurationWorker(policies, client).execute(await policies.claim())
        assert len(calls) == 1
        assert not await repository.query('SELECT * FROM configuration_proposals')
        assert (await repository.query('SELECT status FROM configuration_requests'))[0]['status'] == 'failed'
        assert (await policies.get(88, 123)).mode == 'summary_only'
    finally:
        await client.close()


@pytest.mark.parametrize('first', [completion(''), completion('{'), completion('{}'),
    completion('{"action":"clarify","message":"x","group_id":999}'),
    completion('') | {'choices': []},
    {'choices': [{'index': 0, 'finish_reason': 'aborted', 'message': {'role': 'assistant', 'content': ''}}]},
])
async def test_config_output_retry_then_success(first, processor, repository, policies):
    await processor.router.dispatch(command('/config 123 帮我多留意'))
    client, calls = mock_client([first, completion(intent(mode='priority').model_dump_json(exclude_unset=True))])
    try:
        await ConfigurationWorker(policies, client).execute(await policies.claim())
        assert len(calls) == 2
        row = (await repository.query('SELECT * FROM configuration_requests'))[0]
        assert row['retry_count'] == 1 and row['status'] == 'completed'
        assert len(await repository.query('SELECT * FROM configuration_proposals')) == 1
    finally:
        await client.close()


@pytest.mark.parametrize('status,expected_calls', [(503, 2), (429, 2), (401, 1)])
async def test_config_provider_retry_and_safe_error(status, expected_calls, processor, repository, policies, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={'error': {'message': 'secret provider API key user text'}})

    client = DeepSeekClient(DeepSeekConfig(retries=1), '')
    client.client = AsyncOpenAI(api_key='test-only', max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await processor.router.dispatch(command('/config 123 secret user request'))
        await ConfigurationWorker(policies, client).execute(await policies.claim())
        row = (await repository.query('SELECT * FROM configuration_requests'))[0]
        assert row['status'] == 'failed' and 'provider_error' in row['error']
        assert len(calls) == expected_calls and row['retry_count'] == expected_calls - 1
        text = str(await repository.query('SELECT text FROM private_outbox')) + caplog.text
        assert 'secret' not in text and 'ValueError' not in text
        assert '稍后重试' in text
    finally:
        await client.close()


@pytest.mark.parametrize('text,hint', [('/config 999 mode inbox', '/allow add 999'),
    ('/config 不知道哪个群', '无法唯一确定'), ('/config 123 mode invalid', '配置字段格式不合法'),
    ('/config 123 summary_enabled "true"', '配置字段格式不合法'), ('/config 123', '配置字段格式不合法')])
async def test_input_errors_never_queue(text, hint, processor, repository):
    await processor.router.dispatch(command(text))
    assert not await repository.query('SELECT * FROM configuration_requests')
    assert not await repository.query('SELECT * FROM configuration_proposals')
    notice = (await repository.query('SELECT text FROM private_outbox'))[0]['text']
    assert hint in notice and 'config.yaml' not in notice


def test_input_error_not_retryable():
    for code in ConfigErrorCode:
        assert not retryable(ConfigParseError(code))


@pytest.mark.parametrize('change', ['admin', 'self_id', 'authorization'])
async def test_feedback_isolation(change, processor, repository, policies):
    await processor.router.dispatch(command('/config 123 开启消息功能'))
    request = await policies.claim()
    llm = AsyncMock(parse_config=AsyncMock(return_value=ConfigFeedback(action='clarify', message='x')))
    if change == 'admin':
        request['admin_qq'] = 100
    elif change == 'self_id':
        request['self_id'] = 222
    else:
        await repository.authorizations.deactivate(88, 123)
    await ConfigurationWorker(policies, llm).execute(request)
    llm.parse_config.assert_not_awaited()
    assert not await repository.query('SELECT * FROM configuration_proposals')
    assert all('配置意图还不明确' not in r['text'] for r in await repository.query('SELECT * FROM private_outbox'))


@pytest.mark.parametrize('prefix', ['123 ', '高数群'])
async def test_legacy_migration_and_running_update_recovery(prefix, repository, policies):
    before = await set_policy(repository, 'summary_only', alias='高数群')
    await repository.query('ALTER TABLE configuration_requests DROP COLUMN intent_text')
    await repository.query("INSERT INTO configuration_requests(self_id,admin_qq,group_id,target_group_id,message_id,input_text,created_at,status) "
                           "VALUES (88,99,123,123,'legacy',?,1,'running')", (prefix + '123 课程信息帮我多留意',))
    for _ in range(2):
        await repository.db.close()
        await repository.db.open()
    assert await policies.get(88, 123) == before
    await policies.recover()
    request = await policies.claim()
    llm = AsyncMock(parse_config=AsyncMock(return_value=intent(mode='priority')))
    worker = ConfigurationWorker(policies, llm)
    await worker.execute(request)
    await worker.execute(request)
    llm.parse_config.assert_awaited_once()
    assert llm.parse_config.call_args.args[0] == '123 课程信息帮我多留意'
    assert len(await repository.query('SELECT * FROM configuration_proposals')) == 1
    assert len(await repository.query('SELECT * FROM private_outbox')) == 1
    assert await policies.get(88, 123) == before
