import json
from unittest.mock import AsyncMock

import pytest

from app.commands.help import render_help
from app.policies.worker import ConfigurationWorker
from app.storage.policy_repository import PolicyRepository
from app.storage.preference_repository import PreferenceRepository, validate_preference_intent
from app.triage.models import PreferenceChanges, PreferenceIntent, TriagePreferences
from tests.test_inbox_files import command


def preference(**changes):
    return PreferenceIntent(action='update_triage_preferences', changes=PreferenceChanges(**changes), reason='用户明确提出个人偏好')


async def propose(processor, repository, text, intent, message_id=10):
    await processor.router.dispatch(command('/pref ' + text).model_copy(update={'message_id': str(message_id)}))
    policies = PolicyRepository(repository.db, [123])
    request = await policies.claim()
    assert request['kind'] == 'triage_preferences'
    llm = AsyncMock()
    llm.parse_preferences.return_value = intent
    await ConfigurationWorker(policies, llm).execute(request)
    llm.parse_config.assert_not_awaited()
    return policies, PreferenceRepository(policies)


@pytest.mark.parametrize('text,changes,expected', [
    ('考试和调课对我很重要', dict(important_keywords_add=['考试', '调课']), dict(important_keywords=['考试', '调课'])),
    ('123456789 是老师，他的消息需要重点关注', dict(important_senders_add=[123456789]), dict(important_senders=[123456789])),
    ('讲座通常是低优先级', dict(low_priority_keywords_add=['讲座']), dict(low_priority_keywords=['讲座'])),
    ('课程资料正常优先级即可', dict(category_priority_preferences={'material': 'normal'}), dict(category_priority_preferences={'material': 'normal'})),
])
async def test_preference_scenarios_require_confirmation(text, changes, expected, processor, repository):
    policies, preferences = await propose(processor, repository, text, preference(**changes))
    assert await preferences.get(88) == TriagePreferences()
    proposal = (await repository.query('SELECT * FROM configuration_proposals'))[0]
    assert proposal['kind'] == 'triage_preferences'
    diff = json.loads(proposal['intent_json'])
    assert diff['after'] == expected
    assert any('→' in row['text'] for row in await repository.query('SELECT text FROM private_outbox'))
    await processor.router.dispatch(command('/confirm ' + str(proposal['id'])))
    assert (await preferences.get(88)).model_dump() == TriagePreferences().model_dump() | expected
    assert 'confirmed' in await policies.resolve(88, 99, proposal['id'], True)
    assert not await repository.query('SELECT * FROM group_policies')


@pytest.mark.parametrize('operation', ['cancel', 'expire', 'stale'])
async def test_cancel_expiry_and_stale_preferences(operation, processor, repository):
    policies, prefs = await propose(processor, repository, '考试重要', preference(important_keywords_add=['考试']))
    identifier = (await repository.query('SELECT id FROM configuration_proposals'))[0]['id']
    if operation == 'cancel':
        await processor.router.dispatch(command(f'/cancel {identifier}'))
    elif operation == 'expire':
        await repository.query('UPDATE configuration_proposals SET expires_at=0')
        assert '过期' in await policies.resolve(88, 99, identifier, True)
    else:
        await repository.query('INSERT INTO triage_preferences VALUES (88,?,1)', (TriagePreferences(important_keywords=['调课']).model_dump_json(),))
        assert '失效' in await policies.resolve(88, 99, identifier, True)
    assert (await prefs.get(88)).important_keywords == (['调课'] if operation == 'stale' else [])


async def test_prefs_read_and_minimal_diff_keeps_other_fields(processor, repository):
    original = TriagePreferences(important_senders=[12345], low_priority_keywords=['讲座'], category_priority_preferences={'exam': 'high'})
    await repository.query('INSERT INTO triage_preferences VALUES (88,?,1)', (original.model_dump_json(),))
    policies, prefs = await propose(processor, repository, '调课很重要', preference(important_keywords_add=['调课']))
    await policies.resolve(88, 99, 1, True)
    assert (await prefs.get(88)).model_dump() == original.model_dump() | dict(important_keywords=['调课'])
    await processor.router.dispatch(command('/prefs'))
    text = (await repository.query('SELECT text FROM private_outbox ORDER BY id DESC LIMIT 1'))[0]['text']
    assert 'Echelon Preferences' in text and '调课' in text and '12345' in text


async def test_preference_account_and_admin_boundaries(processor, repository):
    policies, prefs = await propose(processor, repository, '考试重要', preference(important_keywords_add=['考试']))
    for self_id, user in ((222, 99), (88, 100)):
        with pytest.raises(ValueError):
            await policies.resolve(self_id, user, 1, True)
    with pytest.raises(ValueError):
        await prefs.get(222)
    await repository.state('onebot_self_id', '222')
    assert await prefs.get(222) == TriagePreferences()
    with pytest.raises(ValueError):
        await policies.resolve(222, 99, 1, True)


@pytest.mark.parametrize('text', ['/prefs', '/pref 考试很重要', '/confirm 1', '/cancel 1'])
async def test_nonadmin_preference_commands_ignored(text, processor, repository):
    await processor.router.dispatch(command(text, user_id=100))
    assert not await repository.query('SELECT * FROM configuration_requests')
    assert not await repository.query('SELECT * FROM private_outbox')


@pytest.mark.parametrize('changes', [dict(important_keywords_add=['未提及词']), dict(important_senders_add=[98765]),
    dict(category_priority_preferences={'exam': 'critical'})])
def test_model_cannot_add_unmentioned_preference(changes):
    with pytest.raises(ValueError):
        validate_preference_intent(preference(**changes), '讲座通常是低优先级')


@pytest.mark.parametrize('changes,text', [(dict(important_keywords_remove=['考试']), '考试重要'),
    (dict(important_keywords_add=['讲座']), '讲座低优先级'),
    (dict(category_priority_preferences={'material': 'critical'}), '课程资料正常优先级即可')])
def test_model_cannot_invert_or_invent_preference_operation(changes, text):
    with pytest.raises(ValueError):
        validate_preference_intent(preference(**changes), text)


async def test_invalid_preference_keeps_database_and_logs_private(processor, repository, caplog):
    await propose(processor, repository, '考试很重要', preference(important_keywords_add=['private-unmentioned-secret']))
    assert not await repository.query('SELECT * FROM configuration_proposals')
    assert not await repository.query('SELECT * FROM triage_preferences')
    assert (await repository.query('SELECT status FROM configuration_requests'))[0]['status'] == 'failed'
    assert 'private-unmentioned-secret' not in caplog.text


def test_preference_remove_and_accumulation_limits():
    current = TriagePreferences(important_keywords=['考试'], important_senders=[123])
    updated = PreferenceChanges(important_keywords_remove=['考试'], category_priority_preferences={'exam': 'high'}).apply(current)
    assert updated.important_keywords == [] and updated.important_senders == [123]
    with pytest.raises(ValueError):
        PreferenceChanges(important_keywords_add=['extra']).apply(TriagePreferences(important_keywords=[str(n) for n in range(30)]))


@pytest.mark.parametrize('name,expected', [('prefs', '只读'), ('pref', 'Before → After'), ('inbox', 'deadline'),
    ('detail', 'revision'), ('config', '/pref')])
def test_phase3_command_help(name, expected):
    assert expected in render_help(name)
