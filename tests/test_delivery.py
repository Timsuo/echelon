import time
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.delivery.models import DeliveryPreferenceIntent, DeliveryPreferences
from app.delivery.preferences import DeliveryPreferenceRepository, parse_local
from app.delivery.repository import DeliveryRepository, fingerprint
from app.delivery.schedule import next_digest, quiet_end
from app.policies.worker import ConfigurationWorker
from app.storage.policy_repository import PolicyRepository
from app.storage.triage_repository import TriageRepository
from app.triage.models import TriageResult
from tests.conftest import event
from tests.policy_helpers import set_policy
from tests.test_inbox_files import command
from tests.test_triage import item


def local(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


async def preferences(repository, **changes):
    prefs = DeliveryPreferences(quiet_hours_enabled=False, **changes)
    await repository.query('INSERT INTO delivery_preferences VALUES (88,?,?) ON CONFLICT(self_id) DO UPDATE SET preferences_json=excluded.preferences_json', (prefs.model_dump_json(), time.time()))
    return prefs


async def revision(repository, processor, config, *, source='realtime', when=None, merge=None, **values):
    await set_policy(repository, 'priority')
    message_id = len(await repository.query('SELECT * FROM messages'))+1
    await processor.handle(event(message_id=message_id, time=int(when or time.time()-1)), ingest_source=source)
    triage = TriageRepository(repository, config)
    job = await triage.claim(time.time()+601)
    messages, _, candidates = await triage.context(job)
    result = TriageResult(items=[item([m['id'] for m in messages], merge_into_item_id=merge, **values)], ignored_message_ids=[])
    await triage.apply(job, result, candidates, False, 'mock')
    return (await repository.query('SELECT * FROM inbox_items ORDER BY id DESC LIMIT 1'))[0]


async def outbox(repository, kind='delivery'):
    return await repository.query('SELECT * FROM private_outbox WHERE producer_kind=?', (kind,))


async def delivered(repository, kind):
    for chunk in await outbox(repository, kind):
        await repository.notification_result(chunk)


async def test_idle_heartbeat_is_silent(repository, config):
    await DeliveryRepository(repository, config).tick(88)
    assert not await outbox(repository)
    assert not await outbox(repository, 'digest')


@pytest.mark.parametrize('priority,watch,archived,removed,expected', [
    ('normal', True, False, False, 0), ('high', True, False, False, 1),
    ('critical', True, False, False, 1), ('high', False, False, False, 0),
    ('critical', True, True, False, 0), ('high', True, False, True, 0)])
async def test_urgent_eligibility(priority, watch, archived, removed, expected, repository, processor, config):
    await preferences(repository)
    await revision(repository, processor, config, priority=priority)
    await set_policy(repository, 'inbox', priority_watch_enabled=watch)
    if archived:
        await repository.query("UPDATE inbox_items SET status='archived'")
    if removed:
        await repository.authorizations.deactivate(88, 123)
    await DeliveryRepository(repository, config).tick(88)
    assert len(await outbox(repository)) == expected
    assert len(await repository.query('SELECT * FROM delivery_revision_state')) == 1


async def test_fingerprint_dedup_escalation_and_material_update(repository, processor, config):
    await preferences(repository)
    delivery = DeliveryRepository(repository, config)
    await revision(repository, processor, config)
    await delivery.tick(88)
    await revision(repository, processor, config, merge=1, summary='仅修改摘要描述。')
    await delivery.tick(88)
    assert len(await outbox(repository)) == 1
    await revision(repository, processor, config, merge=1, priority='critical')
    await delivery.tick(88)
    assert '优先级升级' in (await outbox(repository))[-1]['text']
    future = datetime.fromtimestamp(time.time()+86400, ZoneInfo('Asia/Shanghai')).isoformat()
    await revision(repository, processor, config, merge=1, priority='critical', deadline_at=future, deadline_text='明确未来截止')
    await delivery.tick(88)
    assert len(await outbox(repository)) == 3
    assert '事项更新' in (await outbox(repository))[-1]['text']
    assert '旧截止' in (await outbox(repository))[-1]['text']


def test_fingerprint_ignores_cosmetics():
    base = dict(title='报告：提交！', summary='a', reason='x', confidence=.4, priority='high', category='assignment', deadline_at=None, action_required=1, action_text='交报告')
    assert fingerprint(base) == fingerprint(base | dict(title='报告 提交', summary='b', reason='y', confidence=.9))
    assert fingerprint(base) != fingerprint(base | dict(priority='critical'))


@pytest.mark.parametrize('clock,inside', [('2026-10-01T23:29:59', False), ('2026-10-01T23:30:00', True),
    ('2026-10-02T00:00:00', True), ('2026-10-02T06:59:59', True), ('2026-10-02T07:00:00', False)])
def test_cross_midnight_quiet_hours(clock, inside):
    end = quiet_end(DeliveryPreferences(), local(clock), 'Asia/Shanghai')
    assert (end is not None) is inside
    if inside:
        assert end == local('2026-10-02T07:00:00')


@pytest.mark.parametrize('priority,override,expected', [('high', False, 'deferred'), ('critical', True, 'enqueued'), ('critical', False, 'deferred')])
async def test_quiet_persistence_and_overrides(priority, override, expected, repository, processor, config):
    prefs = DeliveryPreferences(critical_break_quiet_hours=override, digest_enabled=False)
    await repository.query('INSERT INTO delivery_preferences VALUES (88,?,?)', (prefs.model_dump_json(), time.time()))
    await revision(repository, processor, config, priority=priority)
    now = datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=23, minute=45, second=0).timestamp()
    delivery = DeliveryRepository(repository, config)
    await delivery.tick(88, now)
    assert (await repository.query('SELECT status FROM inbox_deliveries'))[0]['status'] == expected
    if expected == 'deferred':
        assert not await outbox(repository)
        await repository.db.close()
        await repository.db.open()
        await delivery.tick(88, quiet_end(prefs, now, config.timezone))
        assert len(await outbox(repository)) == 1


@pytest.mark.parametrize('change', ['archived', 'removed', 'normal', 'deadline_past', 'fingerprint'])
async def test_deferred_revalidation_cancels(change, repository, processor, config):
    prefs = DeliveryPreferences(digest_enabled=False)
    await repository.query('INSERT INTO delivery_preferences VALUES (88,?,?)', (prefs.model_dump_json(), time.time()))
    await revision(repository, processor, config)
    now = datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=23, minute=45, second=0).timestamp()
    delivery = DeliveryRepository(repository, config)
    await delivery.tick(88, now)
    if change == 'removed':
        await repository.authorizations.deactivate(88, 123)
    else:
        updates = {'archived': "status='archived'", 'normal': "priority='normal'", 'deadline_past': 'deadline_at=1', 'fingerprint': "title='changed'"}
        await repository.query('UPDATE inbox_items SET ' + updates[change])
    await delivery.tick(88, quiet_end(prefs, now, config.timezone))
    assert (await repository.query('SELECT status FROM inbox_deliveries'))[0]['status'] == 'cancelled'
    assert not await outbox(repository)


async def test_outbox_sent_before_reconcile_survives_crash(repository, processor, config):
    await preferences(repository)
    await revision(repository, processor, config)
    delivery = DeliveryRepository(repository, config)
    await delivery.tick(88)
    await delivered(repository, 'delivery')
    assert (await repository.query('SELECT status FROM inbox_deliveries'))[0]['status'] == 'enqueued'
    await repository.db.close()
    await repository.db.open()
    await delivery.tick(88)
    assert (await repository.query('SELECT status FROM inbox_deliveries'))[0]['status'] == 'delivered'
    assert len(await outbox(repository)) == 1


@pytest.mark.parametrize('source,age,future,action,expected', [
    ('realtime', 1, False, False, True), ('history_recovery', 1, False, True, True),
    ('history_recovery', 48, False, True, False), ('history_poll', 48, True, True, True),
    ('history_recovery', 1, False, False, False)])
async def test_history_freshness_uses_event_time(source, age, future, action, expected, repository, processor, config):
    await preferences(repository)
    extra = {'action_required': action, 'action_text': '提交报告' if action else None}
    if future:
        extra.update(deadline_at=datetime.fromtimestamp(time.time()+86400, ZoneInfo(config.timezone)).isoformat(), deadline_text='未来截止')
    await revision(repository, processor, config, source=source, when=time.time()-age*3600, **extra)
    await DeliveryRepository(repository, config).tick(88)
    sent = await outbox(repository)
    assert bool(sent) == expected
    if expected and source != 'realtime':
        assert '离线期间回补提醒' in sent[0]['text'] and '原消息时间' in sent[0]['text']


async def test_coverage_warning_for_alert_and_empty_digest(repository, processor, config):
    await preferences(repository, send_empty_digest=True)
    await revision(repository, processor, config)
    await repository.query("INSERT INTO collection_gaps(self_id,started_at,ended_at,reason,created_at,updated_at) VALUES (88,?,?,'offline',?,?)",
        (time.time()-100, time.time()+1, time.time(), time.time()))
    delivery = DeliveryRepository(repository, config)
    await delivery.tick(88)
    assert '未确认采集缺口' in (await outbox(repository))[0]['text']
    await repository.query("UPDATE inbox_items SET status='archived'")
    await delivery.manual(88, 'manual')
    await delivery.tick(88)
    text = (await outbox(repository, 'digest'))[0]['text']
    assert '当前已采集信息中' in text and '未确认采集缺口' in text and '今天没有重要消息' not in text


@pytest.mark.parametrize('include_read', [False, True])
async def test_digest_read_preference(include_read, repository, processor, config):
    await preferences(repository, digest_include_read=include_read)
    await revision(repository, processor, config, priority='normal')
    await repository.query("UPDATE inbox_items SET status='read'")
    delivery = DeliveryRepository(repository, config)
    await delivery.manual(88, 'manual')
    await delivery.tick(88)
    assert bool(await outbox(repository, 'digest')) == include_read


async def test_digest_once_after_urgent_and_again_after_revision(repository, processor, config):
    await preferences(repository)
    delivery = DeliveryRepository(repository, config)
    await revision(repository, processor, config)
    await delivery.tick(88)
    await delivery.manual(88, 'one')
    await delivery.tick(88)
    assert '[已提前提醒]' in (await outbox(repository, 'digest'))[0]['text']
    await delivered(repository, 'digest')
    await delivery.tick(88)
    await delivery.manual(88, 'two')
    await delivery.tick(88)
    assert len(await outbox(repository, 'digest')) == 1
    await revision(repository, processor, config, merge=1, title='修订后的提交事项')
    await delivery.manual(88, 'three')
    await delivery.tick(88)
    assert len(await outbox(repository, 'digest')) == 2
    assert [r['item_revision'] for r in await repository.query('SELECT * FROM digest_run_items')] == [1, 2]


@pytest.mark.parametrize('age,expected', [(60, 'scheduled'), (100*60, 'catchup'), (181*60, None)])
async def test_schedule_catchup_window_and_restart(age, expected, repository, config):
    await preferences(repository, digest_times=['12:20'], send_empty_digest=True)
    scheduled = local('2026-10-02T12:20:00')
    await repository.query('UPDATE delivery_settings SET delivery_start_at=?,schedule_cursor=?', (scheduled-86400, scheduled-86400))
    delivery = DeliveryRepository(repository, config)
    await delivery.tick(88, scheduled+age)
    runs = await repository.query('SELECT * FROM digest_runs')
    assert ([r['kind'] for r in runs]) == ([expected] if expected else [])
    await repository.db.close()
    await repository.db.open()
    await delivery.tick(88, scheduled+age+1)
    assert len(await repository.query('SELECT * FROM digest_runs')) == len(runs)


async def test_multiple_missed_slots_coalesce(repository, config):
    await preferences(repository, digest_times=['12:00', '12:20', '13:00'], send_empty_digest=True)
    now = local('2026-10-02T14:00:00')
    await repository.query('UPDATE delivery_settings SET delivery_start_at=?,schedule_cursor=?', (now-86400, now-86400))
    await DeliveryRepository(repository, config).tick(88, now)
    runs = await repository.query('SELECT * FROM digest_runs')
    assert len(runs) == 1 and runs[0]['kind'] == 'catchup'


async def test_upgrade_cutoff_no_old_inbox_flood(repository, processor, config):
    await preferences(repository)
    await revision(repository, processor, config)
    await repository.query('UPDATE inbox_revision_events SET created_at=1')
    delivery = DeliveryRepository(repository, config)
    await delivery.tick(88)
    await delivery.manual(88, 'manual')
    await delivery.tick(88)
    assert not await outbox(repository)
    assert not await outbox(repository, 'digest')


@pytest.mark.parametrize('hint', ['keyword', 'sender'])
async def test_fast_track_keeps_correction_debounce_and_does_not_send(hint, repository, processor, config):
    await preferences(repository)
    await set_policy(repository, 'inbox')
    if hint == 'sender':
        from app.triage.models import TriagePreferences
        await repository.query('INSERT INTO triage_preferences VALUES (88,?,0)', (TriagePreferences(important_senders=[99]).model_dump_json(),))
    await processor.handle(event(message='今天截止' if hint == 'keyword' else 'notice'))
    await repository.query('UPDATE triage_message_state SET created_at=1000')
    delivery = DeliveryRepository(repository, config)
    await delivery.tick(88, 1001)
    assert not await outbox(repository)
    triage = TriageRepository(repository, config)
    assert await triage.claim(1029) is None
    await processor.handle(event(message_id=2, message='更正：明天才截止'))
    await repository.query('UPDATE triage_message_state SET created_at=1025 WHERE message_id=2')
    assert await triage.claim(1030) is None
    job = await triage.claim(1055)
    assert len((await triage.context(job))[0]) == 2


@pytest.mark.parametrize('changes', [{'quiet_start': '24:00'}, {'quiet_end': '7:00'}, {'fast_track_debounce_seconds': 0},
    {'heartbeat_seconds': True}, {'digest_times': []}, {'digest_times': [f'{i:02d}:00' for i in range(13)]}, {'enabled': 'true'}, {'urgent_priorities': ['normal']}])
def test_delivery_preferences_strict(changes):
    with pytest.raises(ValidationError):
        DeliveryPreferences(**changes)


def test_times_normalized_deduped_and_next():
    prefs = DeliveryPreferences(digest_times=['12:20', '07:30', '12:20'])
    assert prefs.digest_times == ['07:30', '12:20']
    assert next_digest(prefs, local('2026-10-02T12:21'), 'Asia/Shanghai') == local('2026-10-03T07:30')
    assert parse_local('每天7:30、12:20收信').changes.digest_times == ['07:30', '12:20']


async def test_delivery_commands_confirm_cancel_expire_and_global_target(repository, processor):
    policies = PolicyRepository(repository.db)
    pref = DeliveryPreferenceRepository(policies)
    await processor.router.dispatch(command('/notify 每天08:00、20:00收信'))
    assert (await pref.get(88)).digest_times != ['08:00', '20:00']
    await processor.router.dispatch(command('/confirm 1'))
    assert (await pref.get(88)).digest_times == ['08:00', '20:00']
    await processor.router.dispatch(command('/notify critical 不要打破静默'))
    await processor.router.dispatch(command('/cancel 2'))
    assert (await pref.get(88)).critical_break_quiet_hours
    await processor.router.dispatch(command('/notify 每天09:00收信'))
    await repository.query('UPDATE configuration_proposals SET expires_at=0 WHERE id=3')
    await processor.router.dispatch(command('/confirm 3'))
    assert (await pref.get(88)).digest_times == ['08:00', '20:00']
    await processor.router.dispatch(command('/notify 晚上十一点半到早上七点不要提醒我'))
    request = await policies.claim()
    assert request['target_group_id'] is None
    llm = AsyncMock()
    llm.parse_delivery_preferences.return_value = DeliveryPreferenceIntent(action='update_delivery_preferences', changes={'quiet_end': '06:00'}, reason='测试')
    await ConfigurationWorker(policies, llm).execute(request)
    await processor.router.dispatch(command('/delivery'))
    assert 'Heartbeat' in (await repository.query('SELECT text FROM private_outbox ORDER BY id DESC LIMIT 1'))[0]['text']


@pytest.mark.parametrize('bad', [{'group_id': 123}, {'cron': '* * * * *'}, {'path': 'C:/'}, {'mode': 'priority'}])
def test_delivery_intent_cannot_escape_preferences(bad):
    with pytest.raises(ValidationError):
        DeliveryPreferenceIntent(action='update_delivery_preferences', changes=bad, reason='bad')


async def test_revision_events_only_on_material_changes(repository, processor, config):
    await revision(repository, processor, config)
    await revision(repository, processor, config, merge=1, reason='新的简短依据', confidence=.8)
    assert len(await repository.query('SELECT * FROM inbox_revision_events')) == 1
    await revision(repository, processor, config, merge=1, title='新标题')
    assert len(await repository.query('SELECT * FROM inbox_revision_events')) == 2


async def test_delivery_self_id_isolation(repository, config):
    with pytest.raises(ValueError):
        await DeliveryRepository(repository, config).tick(222)
    assert not await repository.query('SELECT * FROM inbox_deliveries')


async def test_outbox_chunks_one_producer_and_idempotent(repository):
    text = '📝中文📅' * 1000
    async with repository.db.transaction() as connection:
        for _ in range(2):
            await repository.enqueue_text(connection, text, 88, producer_kind='test', producer_key='one')
    chunks = await outbox(repository, 'test')
    rebuilt = ''.join(row['text'].split('\n', 1)[1] for row in chunks)
    assert rebuilt == text
    assert len({row['producer_key'] for row in chunks}) == 1
    assert all(len(row['text']) <= 2000 for row in chunks)
