import asyncio
import json
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from app.config import TriageConfig
from app.storage.inbox_repository import InboxRepository
from app.storage.triage_repository import TriageRepository
from app.triage.models import TriageItem, TriageResult
from app.triage.worker import TriageWorker
from tests.conftest import event
from tests.policy_helpers import set_policy
from tests.test_attachments import file_event


def item(ids, **overrides):
    return TriageItem.model_validate(dict(source_message_ids=ids, merge_into_item_id=None,
        title='实验报告', summary='周五提交实验报告 PDF', category='assignment', priority='high',
        labels=['assignment', 'submission'], action_required=True, action_text='提交实验报告 PDF',
        deadline_text=None, deadline_at=None, confidence=0.91, reason='老师明确要求提交报告。') | overrides)


async def candidate(repository, config, processor, count=1, **overrides):
    await set_policy(repository)
    for index in range(count):
        await processor.handle(event(message_id=index + 1, **overrides))
    repo = TriageRepository(repository, config)
    job = await repo.claim(time.time() + 601)
    messages, _, candidates = await repo.context(job)
    return repo, job, [m['id'] for m in messages], candidates


@pytest.mark.parametrize('mode,expected', [('summary_only', 0), ('ignore', 0), ('inbox', 1), ('priority', 1)])
async def test_policy_scheduling(mode, expected, repository, processor, config):
    await set_policy(repository, mode)
    await processor.handle(event())
    assert len(await repository.query('SELECT * FROM triage_message_state')) == expected
    assert bool(await TriageRepository(repository, config).claim(time.time() + 601)) == bool(expected)


async def test_disabled_and_policy_paused_do_not_call_model(repository, processor, config):
    repository.triage_config = TriageConfig(enabled=False)
    await set_policy(repository)
    await processor.handle(event())
    assert not await repository.query('SELECT * FROM triage_message_state')
    repository.triage_config = TriageConfig()
    await processor.handle(event(message_id=2))
    await set_policy(repository, 'summary_only')
    assert await TriageRepository(repository, config).claim(time.time() + 601) is None
    assert (await repository.query('SELECT status FROM triage_message_state'))[0]['status'] == 'pending'


async def test_duplicate_realtime_and_history_ownership(repository, processor, config):
    await set_policy(repository)
    for source in ('realtime', 'realtime', 'history_recovery', 'history_poll'):
        await processor.handle(event(), ingest_source=source)
    repo = TriageRepository(repository, config)
    jobs = await asyncio.gather(repo.claim(time.time() + 601), repo.claim(time.time() + 601))
    assert sum(job is not None for job in jobs) == 1
    assert len(await repository.query('SELECT * FROM triage_message_state')) == 1
    assert len(await repository.query('SELECT * FROM triage_job_messages')) == 1


@pytest.mark.parametrize('mode,debounce,maximum', [('inbox', 180, 600), ('priority', 60, 180)])
async def test_debounce_and_hard_max_wait(mode, debounce, maximum, repository, processor, config):
    await set_policy(repository, mode)
    await processor.handle(event())
    await processor.handle(event(message_id=2))
    await repository.query('UPDATE triage_message_state SET created_at=1000 WHERE message_id=1')
    await repository.query('UPDATE triage_message_state SET created_at=? WHERE message_id=2', (1000 + maximum - 10,))
    repo = TriageRepository(repository, config)
    assert await repo.claim(1000 + maximum - 1) is None
    job = await repo.claim(1000 + maximum)
    assert len((await repo.context(job))[0]) == 2
    await processor.handle(event(message_id=3))
    await repository.query('UPDATE triage_message_state SET created_at=2000 WHERE message_id=3')
    assert await repo.claim(2000 + debounce - 1) is None
    assert await repo.claim(2000 + debounce) is not None


async def test_candidate_limits_and_group_isolation(repository, processor, config):
    config.triage.max_messages_per_candidate = 2
    await repository.authorizations.activate(88, 456)
    for group in (123, 456):
        await set_policy(repository, group_id=group)
        for index in range(3):
            await processor.handle(event(group_id=group, message_id=index + 1))
    repo = TriageRepository(repository, config)
    jobs = [await repo.claim(time.time()), await repo.claim(time.time())]
    assert {j['group_id'] for j in jobs} == {123, 456}
    for job in jobs:
        messages = (await repo.context(job))[0]
        assert len(messages) == 2
        assert {m['group_id'] for m in messages} == {job['group_id']}


async def test_span_and_chars_split_without_dropping_messages(repository, processor, config):
    await set_policy(repository)
    for index, timestamp in enumerate((1000, 1001, 9000), 1):
        await processor.handle(event(message_id=index, time=timestamp, message='x' * 10000))
    repo = TriageRepository(repository, config)
    jobs = [await repo.claim(time.time() + 601) for _ in range(3)]
    assert all(jobs)
    assert len(await repository.query('SELECT * FROM triage_job_messages')) == 3
    for job in jobs:
        assert job['window_end'] - job['window_start'] <= config.triage.max_span_seconds + 1


async def test_account_isolation_and_composite_constraints(repository, processor, config):
    await set_policy(repository)
    await set_policy(repository, self_id=222)
    await processor.handle(event())
    await processor.handle(event(self_id=222))
    repo = TriageRepository(repository, config)
    job = await repo.claim(time.time() + 601)
    assert job['self_id'] == 88
    assert await repo.claim(time.time() + 601) is None
    other = (await repository.query('SELECT id FROM messages WHERE self_id=222'))[0]['id']
    with pytest.raises(sqlite3.IntegrityError):
        await repository.query('INSERT INTO triage_job_messages VALUES (?,?,?,?)', (job['id'], other, 88, 123))
    with pytest.raises(sqlite3.IntegrityError):
        await repository.query('INSERT INTO triage_message_state(message_id,self_id,group_id,status,triage_job_id,created_at) VALUES (?,?,?, ?,NULL,1)', (999, 88, 123, 'pending'))


async def test_restart_preserves_ownership_and_attempt_budget(repository, processor, config):
    repo, job, ids, _ = await candidate(repository, config, processor)
    await repo.recover()
    recovered = await repo.claim(time.time() + 601)
    assert recovered['id'] == job['id'] and recovered['attempts'] == 2
    assert [m['id'] for m in (await repo.context(recovered))[0]] == ids


async def test_old_messages_not_retroactively_scheduled(repository, processor, config):
    await processor.handle(event())  # summary_only at insertion
    await set_policy(repository)
    await processor.handle(event(), ingest_source='history_recovery')
    assert not await repository.query('SELECT * FROM triage_message_state')
    await processor.handle(event(message_id=2, time=100), ingest_source='history_recovery')
    repo = TriageRepository(repository, config)
    job = await repo.claim(time.time() + 601)
    assert job['source_kind'] == 'history_recovery'
    assert [m['message_id'] for m in (await repo.context(job))[0]] == ['2']


async def test_multiple_items_and_sources_atomic(repository, processor, config):
    repo, job, ids, candidates = await candidate(repository, config, processor, count=3)
    result = TriageResult(items=[item(ids[:2]), item([ids[2]], title='调课', category='schedule')], ignored_message_ids=[])
    await repo.apply(job, result, candidates, False, 'mock')
    assert len(await repository.query('SELECT * FROM inbox_items')) == 2
    assert len(await repository.query('SELECT * FROM inbox_item_messages')) == 3
    assert {r['status'] for r in await repository.query('SELECT * FROM triage_message_state')} == {'processed'}
    saved = (await repository.query('SELECT * FROM triage_jobs'))[0]
    assert saved['status'] == 'completed' and json.loads(saved['result_json']) == result.model_dump()
    assert not await repository.query('SELECT * FROM private_outbox')  # No automatic alerts.


async def test_empty_result_explicitly_ignores_chatter(repository, processor, config):
    repo, job, ids, candidates = await candidate(repository, config, processor, count=2)
    await repo.apply(job, TriageResult(items=[], ignored_message_ids=ids), candidates, False, 'mock')
    assert not await repository.query('SELECT * FROM inbox_items')
    assert {r['status'] for r in await repository.query('SELECT * FROM triage_message_state')} == {'ignored'}


async def test_merge_revision_read_and_material_change(repository, processor, config):
    repo, job, ids, candidates = await candidate(repository, config, processor)
    await repo.apply(job, TriageResult(items=[item(ids)], ignored_message_ids=[]), candidates, False, 'mock')
    inbox = InboxRepository(repository.db)
    original = await inbox.detail(88, 1, mark_read=True)
    assert original['revision'] == 1 and original['status'] == 'read'
    for number, title, expected in ((2, '实验报告', 1), (3, '实验报告更新', 2)):
        await processor.handle(event(message_id=number))
        job = await repo.claim(time.time() + 601)
        messages, _, candidates = await repo.context(job)
        result = TriageResult(items=[item([m['id'] for m in messages], merge_into_item_id=1, title=title)], ignored_message_ids=[])
        await repo.apply(job, result, candidates, False, 'mock')
        detail = await inbox.detail(88, 1, mark_read=True)
        assert detail['revision'] == expected
        assert detail['model_priority'] == detail['priority'] == 'high'
        assert detail['labels'] == ['assignment', 'submission']
    assert len(await repository.query('SELECT * FROM inbox_item_messages')) == 3


async def test_file_placeholder_reused_and_attachment_metadata_only(repository, processor, config):
    await set_policy(repository)
    await processor.handle(file_event())
    await processor.handle(event(message_id=2, message='这是实验报告模板'))
    repo = TriageRepository(repository, config)
    job = await repo.claim(time.time() + 601)
    messages, attachments, candidates = await repo.context(job)
    assert candidates[0]['id'] == 1
    assert set(attachments[0]) == {'message_id', 'filename', 'file_size', 'download_status'}
    # Even if model proposes a new item, Python deterministically reuses the linked file placeholder.
    await repo.apply(job, TriageResult(items=[item([m['id'] for m in messages])], ignored_message_ids=[]), candidates, False, 'mock')
    items = await repository.query('SELECT * FROM inbox_items')
    assert len(items) == 1 and items[0]['revision'] == 2
    detail = await InboxRepository(repository.db).detail(88, 1)
    assert detail['message_count'] == 2 and len(detail['attachments']) == 1


@pytest.mark.parametrize('change', ['archive', 'account', 'group', 'revision'])
async def test_merge_rechecked_after_model_call(change, repository, processor, config):
    repo, job, ids, candidates = await candidate(repository, config, processor)
    await repo.apply(job, TriageResult(items=[item(ids)], ignored_message_ids=[]), candidates, False, 'mock')
    await processor.handle(event(message_id=2))
    job = await repo.claim(time.time() + 601)
    messages, _, candidates = await repo.context(job)
    sql = {'archive': "status='archived'", 'account': 'self_id=222', 'group': 'source_group_id=456', 'revision': 'revision=revision+1'}[change]
    if change == 'account':
        await repository.state('onebot_self_id', '222')
    else:
        await repository.query('UPDATE inbox_items SET ' + sql + ' WHERE id=1')
    with pytest.raises((ValueError, PermissionError)):
        await repo.apply(job, TriageResult(items=[item([m['id'] for m in messages], merge_into_item_id=1)], ignored_message_ids=[]), candidates, False, 'mock')
    assert (await repository.query('SELECT status FROM triage_jobs WHERE id=?', (job['id'],)))[0]['status'] == 'running'


async def test_result_application_rolls_back_everything(repository, processor, config, monkeypatch):
    repo, job, ids, candidates = await candidate(repository, config, processor)
    original = repository.db.connection.execute
    def execute(sql, *args, **kwargs):
        if 'INSERT INTO inbox_item_labels' in sql:
            raise RuntimeError('injected disk failure')
        return original(sql, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(repository.db.connection, 'execute', execute)
        with pytest.raises(RuntimeError):
            await repo.apply(job, TriageResult(items=[item(ids)], ignored_message_ids=[]), candidates, False, 'mock')
    assert not await repository.query('SELECT * FROM inbox_items')
    assert not await repository.query('SELECT * FROM inbox_item_messages')
    assert (await repository.query('SELECT status FROM triage_message_state'))[0]['status'] == 'queued'
    assert (await repository.query('SELECT result_json FROM triage_jobs'))[0]['result_json'] is None


@pytest.mark.parametrize('coverage', ['partial', 'unknown', 'failed', 'pending', 'running', 'likely_covered'])
async def test_coverage_is_python_decision(coverage, repository, processor, config):
    repo, job, ids, _ = await candidate(repository, config, processor, time=1000)
    await repository.query("INSERT INTO collection_gaps(id,self_id,started_at,ended_at,reason,recovery_status,created_at,updated_at) VALUES (1,88,900,1100,'test',?,1,1)", (coverage,))
    await repository.query("INSERT INTO history_sync_jobs(self_id,group_id,gap_id,mode,window_start,window_end,status,coverage,created_at) VALUES (88,123,1,'reconnect',900,1100,?,?,1)",
                           ('completed' if coverage == 'likely_covered' else 'running', coverage))
    llm = AsyncMock()
    llm.triage.return_value = TriageResult(items=[item(ids)], ignored_message_ids=[])
    await TriageWorker(repo, llm).execute(job)
    detail = await InboxRepository(repository.db).detail(88, 1)
    assert detail['coverage_status'] == ('no_known_gap' if coverage == 'likely_covered' else 'warning')
    assert 'coverage_status' not in json.loads(llm.triage.call_args.args[0])


async def test_delayed_llm_does_not_block_collector(repository, processor, config):
    repo, job, ids, _ = await candidate(repository, config, processor)
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(text):
        entered.set()
        await release.wait()
        return TriageResult(items=[item(ids)], ignored_message_ids=[])
    llm = AsyncMock()
    llm.triage.side_effect = delayed
    task = asyncio.create_task(TriageWorker(repo, llm).execute(job))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        await asyncio.wait_for(processor.handle(event(message_id=2)), 2)
        assert len(await repository.query('SELECT * FROM messages')) == 2
    finally:
        release.set()
        await task


async def test_finite_output_retry_and_failure_preserves_messages(repository, processor, config):
    from app.llm.deepseek import RetryableModelOutputError
    repo, job, _, _ = await candidate(repository, config, processor)
    llm = AsyncMock()
    llm.triage.side_effect = RetryableModelOutputError('empty')
    worker = TriageWorker(repo, llm)
    for _ in range(3):
        await worker.execute(job)
        await repo.recover()
        job = await repo.claim(time.time() + 601)
    assert job is None and llm.triage.await_count == 3
    assert (await repository.query('SELECT status FROM triage_jobs'))[0]['status'] == 'failed'
    assert (await repository.query('SELECT status FROM triage_message_state'))[0]['status'] == 'failed'
    await processor.handle(event(message_id=2))
    assert len(await repository.query('SELECT * FROM messages')) == 2


async def test_mixed_source_kind_and_preferences_reach_model(repository, processor, config):
    from app.triage.models import TriagePreferences
    await set_policy(repository)
    await processor.handle(event(time=1000))
    await processor.handle(event(message_id=2, time=1001), ingest_source='history_poll')
    prefs = TriagePreferences(important_keywords=['考试'])
    await repository.query('INSERT INTO triage_preferences VALUES (88,?,1)', (prefs.model_dump_json(),))
    repo = TriageRepository(repository, config)
    job = await repo.claim(time.time() + 601)
    assert job['source_kind'] == 'mixed'
    llm = AsyncMock()
    llm.triage.return_value = TriageResult(items=[], ignored_message_ids=[1, 2])
    await TriageWorker(repo, llm).execute(job)
    data = json.loads(llm.triage.call_args.args[0])
    assert data['preferences'] == prefs.model_dump()
    assert {m['event_time'] for m in data['messages']} == {1000, 1001}
    assert {m['ingest_source'] for m in data['messages']} == {'realtime', 'history_poll'}
    assert data['timezone'] == 'Asia/Shanghai'


@pytest.mark.parametrize('kind,expected_calls', [('length', 1), ('invented', 3)])
async def test_nonretryable_length_and_retryable_reference_error(kind, expected_calls, repository, processor, config):
    from app.llm.deepseek import NonRetryableModelOutputError
    repo, job, _, _ = await candidate(repository, config, processor)
    llm = AsyncMock()
    if kind == 'length':
        llm.triage.side_effect = NonRetryableModelOutputError('length')
    else:
        llm.triage.return_value = TriageResult(items=[item([999])], ignored_message_ids=[])
    for _ in range(expected_calls):
        await TriageWorker(repo, llm).execute(job)
        job = await repo.claim(time.time() + 601)
    assert job is None and llm.triage.await_count == expected_calls
    assert not await repository.query('SELECT * FROM inbox_items')
    assert (await repository.query('SELECT status FROM triage_message_state'))[0]['status'] == 'failed'


@pytest.mark.parametrize('argument,expected', [('high', ['high']), ('critical', ['critical']),
    ('deadline', ['high']), ('action', ['critical']), ('', ['high', 'critical', 'normal']), ('unread', ['high', 'critical', 'normal'])])
async def test_inbox_filters_are_scoped_and_exclude_archived(argument, expected, processor, repository):
    from tests.test_inbox_files import command
    for self_id, priority, status in ((88, 'high', 'unread'), (88, 'critical', 'unread'),
                                      (88, 'normal', 'unread'), (88, 'high', 'archived'), (222, 'high', 'unread')):
        await repository.query('INSERT INTO inbox_items(self_id,title,priority,status,deadline_text,action_required,created_at,updated_at) VALUES (?,?,?,?,?,?,1,1)',
            (self_id, 'item-' + priority, priority, status, '周五' if priority == 'high' else None, int(priority == 'critical')))
    await processor.router.dispatch(command('/inbox ' + argument))
    text = (await repository.query('SELECT text FROM private_outbox ORDER BY id DESC LIMIT 1'))[0]['text']
    for priority in ('high', 'critical', 'normal'):
        assert text.count('item-' + priority) == int(priority in expected)


async def test_detail_renders_triage_and_read_does_not_change_revision(repository, processor, config):
    from tests.test_inbox_files import command
    repo, job, ids, candidates = await candidate(repository, config, processor)
    await repo.apply(job, TriageResult(items=[item(ids, deadline_text='周五前')], ignored_message_ids=[]), candidates, True, 'mock')
    await processor.router.dispatch(command('/detail 1'))
    text = (await repository.query('SELECT text FROM private_outbox ORDER BY id DESC LIMIT 1'))[0]['text']
    for part in ('HIGH', 'ASSIGNMENT', '#submission', '周五前', '提交实验报告 PDF', '0.91', '未确认采集缺口'):
        assert part in text
    saved = (await repository.query('SELECT * FROM inbox_items'))[0]
    assert saved['revision'] == 1 and saved['status'] == 'read'


async def test_oversize_source_fails_explicitly_not_silently_truncated(repository, processor, config):
    repo, job, _, _ = await candidate(repository, config, processor, message='x' * 50000)
    llm = AsyncMock()
    await TriageWorker(repo, llm).execute(job)
    llm.triage.assert_not_awaited()
    assert (await repository.query('SELECT status FROM triage_message_state'))[0]['status'] == 'failed'
    assert len((await repository.query('SELECT normalized_text FROM messages'))[0]['normalized_text']) == 50000


async def test_completed_result_cannot_be_applied_twice(repository, processor, config):
    repo, job, ids, candidates = await candidate(repository, config, processor)
    result = TriageResult(items=[item(ids)], ignored_message_ids=[])
    await repo.apply(job, result, candidates, False, 'mock')
    with pytest.raises(ValueError):
        await repo.apply(job, result, candidates, False, 'mock')
    await repo.fail(job, 'StaleExecutionError', False)
    assert len(await repository.query('SELECT * FROM inbox_items')) == 1
    assert (await repository.query('SELECT status FROM triage_message_state'))[0]['status'] == 'processed'


async def test_stale_attempt_cannot_apply_or_fail_reclaimed_job(repository, processor, config):
    repo, stale, ids, candidates = await candidate(repository, config, processor)
    await repo.recover()
    current = await repo.claim(time.time() + 601)
    with pytest.raises(ValueError):
        await repo.apply(stale, TriageResult(items=[item(ids)], ignored_message_ids=[]), candidates, False, 'mock')
    await repo.fail(stale, 'OldAttempt', False)
    assert (await repository.query('SELECT status FROM triage_jobs'))[0]['status'] == 'running'
    await repo.apply(current, TriageResult(items=[item(ids)], ignored_message_ids=[]), candidates, False, 'mock')


async def test_multiple_file_placeholders_consolidated_without_losing_links(repository, processor, config):
    await set_policy(repository)
    await processor.handle(event(message=[{'type': 'file', 'data': {'file_id': f'f{n}', 'file': f'{n}.txt'}} for n in (1, 2)]))
    repo = TriageRepository(repository, config)
    job = await repo.claim(time.time() + 601)
    messages, _, candidates = await repo.context(job)
    await repo.apply(job, TriageResult(items=[item([m['id'] for m in messages])], ignored_message_ids=[]), candidates, False, 'mock')
    items = await InboxRepository(repository.db).list_items(88, False, 10)
    assert len(items) == 1
    detail = await InboxRepository(repository.db).detail(88, items[0]['id'])
    assert len(detail['attachments']) == 2
    assert len(await repository.query('SELECT * FROM attachments')) == 2
    assert len(await repository.query("SELECT * FROM inbox_items WHERE status='archived'")) == 1
