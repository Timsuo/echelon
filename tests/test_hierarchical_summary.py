import json
import time
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.commands.saved_summaries import SavedSummaryCommands, group_name
from app.config import SummaryConfig
from app.delivery.repository import DeliveryRepository
from app.inbox.renderer import render_inbox
from app.jobs.service import SummaryService
from app.llm.prompts import SYSTEM_PROMPT
from app.llm.schemas import (
    LegacySummaryData,
    LegacyTopic,
    SummaryData,
    Topic,
    add_topic_times,
    load_summary,
)
from app.notifier.renderer import (
    make_compact,
    participants,
    render_compact,
    render_summary,
    time_range,
)
from app.rendering.icons import CATEGORY_ICON, PRIORITY_ICON
from tests.conftest import event
from tests.policy_helpers import set_policy
from tests.test_delivery import outbox, preferences
from tests.test_inbox_files import command


def result(count=1, long=False, **changes):
    topics = [Topic(title=f'讨论 {i}', summary=('重点。'+'后续详细说明。'*150 if long else '周四地点改为B305。'),
        participants=['张老师', '小明'], start_message_id='1', end_message_id='2', category='schedule',
        importance='high' if i == 0 else 'normal') for i in range(count)]
    return SummaryData(topics=topics, decisions=['决定换教室'], todos=['周五交报告'],
        important_events=['模板已上传'], uncertainties=['命名待确认'], notable_message_ids=['1'], **changes)


def messages():
    return [{'message_id': '1', 'event_time': 1790834580}, {'message_id': '2', 'event_time': 1790835480}]


async def generate(repository, processor, config, data=None, *, start=None, end=None, cmd='gen'):
    now = int(time.time())
    await processor.handle(event(message_id=1, time=now-100))
    await processor.handle(event(message_id=2, time=now-10))
    await repository.queue_summaries(88, cmd, [123], start or now-1000, end or now)
    llm = AsyncMock()
    llm.summarize.return_value = data or result()
    await SummaryService(repository, llm, config).execute(await repository.claim_job())
    found = await repository.query('SELECT * FROM summaries ORDER BY id DESC LIMIT 1')
    return found[0] if found else None, llm


def test_topic_time_provenance_and_duration():
    data = add_topic_times(result(), messages())
    topic = data.topics[0]
    assert topic.topic_start == messages()[0]['event_time']
    assert topic.topic_end == messages()[1]['event_time']
    assert topic.duration_seconds == 900
    assert load_summary(data.model_dump_json()) == data


@pytest.mark.parametrize('field,value', [('start_message_id', 'missing'), ('end_message_id', 'missing'), ('notable_message_ids', ['missing'])])
def test_topic_invalid_reference_fails(field, value):
    data = result()
    setattr(data.topics[0], field, value)
    with pytest.raises(ValueError):
        add_topic_times(data, messages())


def test_topic_reverse_and_zero_duration():
    data = result()
    data.topics[0].start_message_id = '2'
    assert add_topic_times(data, messages()).topics[0].duration_seconds == 0
    data.topics[0].end_message_id = '1'
    with pytest.raises(ValueError):
        add_topic_times(data, messages())


@pytest.mark.parametrize('start,end,zone,expected', [
    ('2026-10-01T14:03+08:00', '2026-10-01T14:18+08:00', 'Asia/Shanghai', '14:03–14:18 · 15分钟'),
    ('2026-10-01T23:50+08:00', '2026-10-02T00:20+08:00', 'Asia/Shanghai', '10月01日 23:50 – 10月02日 00:20 · 30分钟'),
    ('2026-10-01T14:03+08:00', '2026-10-01T14:18+08:00', 'UTC', '06:03–06:18 · 15分钟')])
def test_topic_local_time_format(start, end, zone, expected):
    assert time_range(datetime.fromisoformat(start).timestamp(), datetime.fromisoformat(end).timestamp(), zone) == expected


@pytest.mark.parametrize('field,value', [('topic_start', 10), ('importance', 'critical'), ('category', 'hacked'), ('continuation', 'yes')])
def test_topic_wire_schema_disallows_timestamp_and_arbitrary_semantics(field, value):
    values = result().topics[0].model_dump() | {field: value}
    with pytest.raises(ValidationError):
        Topic.model_validate(values)


async def store_previous(repository, start, end, *, sid=88, gid=123, status='completed', legacy=False):
    job = (await repository.query('INSERT INTO summary_jobs(self_id,group_id,window_start,window_end,status,created_at) VALUES (?,?,?,?,?,?) RETURNING id',
        (sid, gid, start, end, status, time.time())))[0]['id']
    data = LegacySummaryData(topics=[LegacyTopic(title='旧话题', summary='旧背景', participants=[])], decisions=[], todos=[], important_events=[], uncertainties=[], notable_message_ids=[]) if legacy else add_topic_times(result(), messages())
    return (await repository.query('INSERT INTO summaries(job_id,self_id,group_id,window_start,window_end,model,source_message_count,summary_json,rendered_text,created_at,schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id',
        (job, sid, gid, start, end, 'mock', 2, data.model_dump_json(), 'OLD RENDERED NEVER MODEL INPUT', time.time(), 1 if legacy else 2)))[0]['id']


async def test_continuity_same_account_group_completed_nonoverlap_limit_lookback(repository, config):
    start = 1000000
    good = [await store_previous(repository, start-i*100-80, start-i*100) for i in range(1, 5)]
    await store_previous(repository, start-200, start-100, sid=222)
    await store_previous(repository, start-200, start-100, gid=456)
    await store_previous(repository, start-200, start+1)
    await store_previous(repository, start+1, start+200)
    await store_previous(repository, start-200000, start-180000)
    await store_previous(repository, start-200, start-100, status='failed')
    previous = await repository.previous_summaries({'self_id': 88, 'group_id': 123, 'window_start': start}, config.summary)
    assert [p['summary_id'] for p in previous] == good[:3]
    assert all(p['source_type'] == 'untrusted_model_generated_context' for p in previous)
    assert 'OLD RENDERED' not in json.dumps(previous)


async def test_continuity_input_budget_drops_old_context_first(repository, processor, config):
    now = int(time.time())
    config.deepseek.max_input_chars = 1000
    await store_previous(repository, now-3000, now-1000)
    await store_previous(repository, now-4000, now-2000)
    await repository.query("UPDATE summaries SET summary_json=replace(summary_json, '周四地点改为B305。', ?)", ('冗余背景'*350,))
    summary, llm = await generate(repository, processor, config, start=now-500, end=now)
    assert summary is not None
    payload = json.loads(llm.summarize.call_args.args[0])
    assert len(payload['untrusted_chat_records']) == 2
    assert payload['previous_summaries'] == []


async def test_continuity_is_used_when_within_budget(repository, processor, config):
    now = int(time.time())
    old = await store_previous(repository, now-3000, now-1000, legacy=True)
    _, llm = await generate(repository, processor, config, start=now-500, end=now)
    payload = json.loads(llm.summarize.call_args.args[0])
    assert payload['previous_summaries'][0]['summary_id'] == old
    assert '不是权威事实' in SYSTEM_PROMPT and '不得纯 Summary-of-Summary' in SYSTEM_PROMPT


async def test_empty_current_window_cannot_recycle_previous(repository, config):
    await store_previous(repository, 1, 10)
    await repository.queue_summaries(88, 'empty', [123], 11, 20)
    llm = AsyncMock()
    await SummaryService(repository, llm, config).execute(await repository.claim_job())
    llm.summarize.assert_not_awaited()
    saved = (await repository.query('SELECT * FROM summaries ORDER BY id DESC LIMIT 1'))[0]
    assert '没有已采集的新消息' in saved['rendered_text']
    assert json.loads(saved['summary_json'])['topics'] == []


@pytest.mark.parametrize('long', [False, True])
async def test_detailed_always_saved_and_default_view_selected(long, repository, processor, config):
    data = result(count=3 if long else 1, long=long)
    summary, llm = await generate(repository, processor, config, data)
    assert summary['schema_version'] == 2
    assert json.loads(summary['summary_json'])['topics'][0]['summary'] == data.topics[0].summary
    assert summary['compact_json'] and summary['compact_rendered_text']
    sent = ''.join(row['text'] for row in await outbox(repository, 'summary'))
    assert sent == summary['compact_rendered_text' if long else 'rendered_text']
    llm.summarize.assert_awaited_once()
    llm.compress_summary.assert_not_awaited()
    if long:
        assert len(sent) < len(summary['rendered_text'])
        assert f'/summary detail {summary["id"]}' in sent


def test_compact_preserves_actionable_sections_and_omissions():
    data = add_topic_times(result(8, long=True), messages())
    compact = make_compact(data, SummaryConfig(compact_max_topics=2), True)
    for field in ('decisions', 'todos', 'important_events', 'uncertainties'):
        assert getattr(compact, field) == getattr(data, field)
    assert compact.omitted_topic_count == len(data.topics)-len(compact.key_topics)
    assert all(topic.title in {t.title for t in data.topics} for topic in compact.key_topics)
    text = render_compact(compact, dict(id=99, group_id=123, window_start=1, window_end=10), 2, 'UTC')
    assert '参考消息' not in text and '参与' not in text
    assert '未确认采集缺口' in text


async def test_compression_failure_preserves_completed_detailed(repository, processor, config, monkeypatch):
    def fail(*args):
        raise RuntimeError('compression failed')
    monkeypatch.setattr('app.notifier.renderer.make_compact', fail)
    saved, llm = await generate(repository, processor, config, result(3, long=True))
    assert saved and '后续详细说明' in saved['rendered_text']
    assert (await repository.query('SELECT status FROM summary_jobs'))[0]['status'] == 'completed'
    assert '/summary detail' in saved['compact_rendered_text']
    assert json.loads(saved['compact_json'])['omitted_topic_count'] == 3
    llm.summarize.assert_awaited_once()


@pytest.mark.parametrize('subcommand', ['list', 'detail 1', 'compact 1', 'topic 1 1'])
async def test_saved_commands_never_regenerate_and_removed_group_readable(subcommand, repository, processor, config):
    await generate(repository, processor, config)
    await repository.authorizations.deactivate(88, 123)
    await processor.router.dispatch(command('/summary '+subcommand))
    text = (await repository.query('SELECT text FROM private_outbox ORDER BY id DESC LIMIT 1'))[0]['text']
    assert '不存在' not in text
    if subcommand == 'list':
        assert '已停止接收' in text
    assert len(await repository.query('SELECT * FROM summary_jobs')) == 1


@pytest.mark.parametrize('subcommand', ['detail -1', 'compact abc', 'detail 999', 'topic 1 0', 'topic 1 99', 'list extra'])
async def test_saved_command_invalid_arguments(subcommand, repository, processor, config):
    await generate(repository, processor, config)
    with pytest.raises(ValueError):
        await SavedSummaryCommands(repository, config).handle(command(''), subcommand)


async def test_saved_commands_cross_account_blocked(repository, processor, config):
    await generate(repository, processor, config)
    await repository.state('onebot_self_id', '222')
    commands = SavedSummaryCommands(repository, config)
    for subcommand in ('detail 1', 'compact 1', 'topic 1 1'):
        with pytest.raises(ValueError):
            await commands.handle(command('', self_id=222), subcommand)
    await commands.handle(command('', self_id=222), 'list')
    assert '暂无' in (await repository.query('SELECT text FROM private_outbox ORDER BY id DESC LIMIT 1'))[0]['text']


async def test_legacy_detail_exact_and_compact_local(repository, processor, config):
    identifier = await store_previous(repository, 1, 10, legacy=True)
    await processor.router.dispatch(command(f'/summary detail {identifier}'))
    assert (await repository.query('SELECT text FROM private_outbox ORDER BY id DESC LIMIT 1'))[0]['text'] == 'OLD RENDERED NEVER MODEL INPUT'
    await processor.router.dispatch(command(f'/summary compact {identifier}'))
    saved = (await repository.query('SELECT * FROM summaries'))[0]
    assert '旧话题' in saved['compact_rendered_text']
    assert saved['rendered_text'] == 'OLD RENDERED NEVER MODEL INPUT'


async def test_malformed_legacy_compact_retains_coverage_warning(repository, processor, config, monkeypatch):
    identifier = await store_previous(repository, 1, 10, legacy=True)
    await repository.query('UPDATE summaries SET summary_json=? WHERE id=?', ('{}', identifier))
    monkeypatch.setattr('app.commands.saved_summaries.HistoryRepository.unresolved', AsyncMock(return_value=[{'id': 1}]))
    await processor.router.dispatch(command(f'/summary compact {identifier}'))
    text = (await repository.query('SELECT text FROM private_outbox ORDER BY id DESC LIMIT 1'))[0]['text']
    assert '旧结构无法压缩' in text and '未确认采集缺口' in text
    assert f'/summary detail {identifier}' in text


@pytest.mark.parametrize('n', [0, 3, 5, 6, 30])
def test_participant_display_limits(n):
    names = [f'同学{i}' for i in range(n)]
    text = participants(names)
    if n <= 5:
        assert text == '、'.join(names)
    else:
        assert f'等 {n} 人' in text and names[-1] not in text


def test_shared_icons_and_model_emoji_not_used():
    assert PRIORITY_ICON == {'critical': '🚨', 'high': '🟠', 'normal': '🔵', 'low': '⚪'}
    data = result()
    data.topics[0].title = '🚨伪造紧急UI'
    timed = add_topic_times(data, messages())
    text = render_summary(timed, dict(id=1, group_id=123, window_start=1, window_end=10), 2, 'UTC')
    assert '🚨' not in text and CATEGORY_ICON['schedule'] in text
    inbox = render_inbox([dict(id=1, status='unread', title='t', source_group_id=123, event_time=1, priority='high', category='schedule')], 1, 'UTC')
    assert CATEGORY_ICON['schedule'] in inbox


async def test_group_display_name_priority(repository):
    await repository.authorizations.activate(88, 123, '远端群名')
    async with repository.db.transaction() as connection:
        assert (await group_name(connection, 88, 123))[0] == '远端群名'
    await set_policy(repository, 'summary_only', alias='别名')
    async with repository.db.transaction() as connection:
        assert (await group_name(connection, 88, 123))[0] == '别名'


@pytest.mark.parametrize('enabled', [False, True])
async def test_digest_optional_group_overview_is_brief(enabled, repository, processor, config):
    await preferences(repository, digest_include_group_summaries=enabled)
    saved, _ = await generate(repository, processor, config, result(3, long=True))
    delivery = DeliveryRepository(repository, config)
    await delivery.manual(88, 'digest')
    await delivery.tick(88)
    digests = await outbox(repository, 'digest')
    assert bool(digests) == enabled
    if enabled:
        assert '群聊概览' in digests[0]['text'] and f'/summary detail {saved["id"]}' in digests[0]['text']
        assert '后续详细说明' not in digests[0]['text']


async def test_summary_real_id_not_failed_job_id(repository, processor, config):
    await repository.queue_summaries(88, 'bad', [123], 1, 2)
    job = await repository.claim_job()
    await repository.fail_job(job, 'fixture')
    saved, _ = await generate(repository, processor, config)
    assert saved['id'] != saved['job_id']
    assert f"#{saved['id']}】" in saved['rendered_text']
    assert f"/summary detail {saved['id']}" in saved['compact_rendered_text']


async def test_raw_window_limit_cannot_be_bypassed(repository, processor, config):
    config.deepseek.max_messages = 1
    saved, llm = await generate(repository, processor, config)
    assert saved is None
    llm.summarize.assert_not_awaited()


async def test_invalid_source_fails_without_saving_summary(repository, processor, config):
    data = result()
    data.topics[0].end_message_id = 'missing'
    saved, _ = await generate(repository, processor, config, data)
    assert saved is None
    assert (await repository.query('SELECT status FROM summary_jobs'))[0]['status'] == 'failed'
