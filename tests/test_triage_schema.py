import json
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.config import DeepSeekConfig, TriageConfig
from app.llm.deepseek import RetryableModelOutputError
from app.triage.deadlines import validate_deadlines
from app.triage.models import PreferenceIntent, TriagePreferences, TriageResult
from app.triage.prompts import TRIAGE_PROMPT, message_data
from tests.test_deepseek import completion
from tests.test_model_output import mock_client
from tests.test_triage import item


@pytest.mark.parametrize('change', [dict(category='invented'), dict(priority='urgent'), dict(labels=['free_tag']),
    dict(action_required=False, action_text='do it'), dict(action_required=True, action_text=None),
    dict(action_required=False, action_text=''), dict(confidence=1.1), dict(confidence=float('nan')),
    dict(deadline_at='2026-10-03T23:59:00', deadline_text='Friday'), dict(title='x'*201),
    dict(source_message_ids=[True]), dict(labels=['file', 'file']), dict(shell='whoami')])
def test_triage_schema_rejects_invalid_fields(change):
    with pytest.raises(ValidationError):
        item([1], **change)


@pytest.mark.parametrize('items,ignored,merge_ids', [
    ([item([999])], [], set()), ([item([1], merge_into_item_id=999)], [], {42}),
    ([item([1])], [1], set()), ([], [], set()), ([], [1, 999], set()),
    ([item([1], merge_into_item_id=42), item([1], merge_into_item_id=42)], [], {42}),
])
def test_invented_unaccounted_and_conflicting_ids(items, ignored, merge_ids):
    with pytest.raises(ValueError):
        TriageResult(items=items, ignored_message_ids=ignored).validate_references({1}, merge_ids)


def test_same_source_can_support_multiple_distinct_items():
    TriageResult(items=[item([1]), item([1], title='另一个事项')], ignored_message_ids=[]).validate_references({1}, set())


def test_strict_result_and_preference_envelopes():
    with pytest.raises(ValidationError):
        TriageResult.model_validate({'items': [], 'ignored_message_ids': [], 'sql': 'DROP TABLE messages'})
    with pytest.raises(ValidationError):
        TriageResult(items=[item([1])] * 11, ignored_message_ids=[])
    for bad in ({'group_id': 123}, {'quiet_hours': '22:00'}, {'path': '.env'}):
        with pytest.raises(ValidationError):
            PreferenceIntent.model_validate({'action': 'update_triage_preferences', 'changes': bad, 'reason': 'x'})


@pytest.mark.parametrize('field,value', [('important_keywords', ['x']*31), ('low_priority_keywords', ['x'*41]),
    ('important_senders', list(range(1, 32))), ('important_senders', [True]),
    ('category_priority_preferences', {'exam': 'urgent'}), ('important_keywords', [' '])])
def test_preference_limits(field, value):
    with pytest.raises(ValidationError):
        TriagePreferences.model_validate({field: value})


@pytest.mark.parametrize('value', [dict(inbox_debounce_seconds=601), dict(max_messages_per_candidate=0),
    dict(max_input_chars=1000), dict(max_span_seconds=0), dict(retry_count=6)])
def test_triage_config_bounds(value):
    with pytest.raises(ValidationError):
        TriageConfig(**value)


def test_relative_deadline_uses_history_event_time_and_timezone():
    timestamp = int(datetime(2026, 10, 1, 22, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
    message = dict(id=1, user_id=99, nickname='teacher', event_time=timestamp,
                   received_time=timestamp+86400*3, ingest_source='history_recovery', normalized_text='明天下午17:00交报告')
    record = message_data(message, 'Asia/Shanghai')
    assert record['local_time'] == '2026-10-01T22:00:00+08:00'
    assert 'received_time' not in record
    result = TriageResult(items=[item([1], deadline_text='明天下午', deadline_at='2026-10-02T17:00:00+08:00')], ignored_message_ids=[])
    validate_deadlines(result, [message], 'Asia/Shanghai')
    wrong = TriageResult(items=[item([1], deadline_text='明天下午', deadline_at='2026-10-05T17:00:00+08:00')], ignored_message_ids=[])
    with pytest.raises(ValueError):
        validate_deadlines(wrong, [message], 'Asia/Shanghai')
    # An ambiguous time can stay textual; Python does not invent a time of day.
    unknown = TriageResult(items=[item([1], deadline_text='明天下午', deadline_at=None)], ignored_message_ids=[])
    validate_deadlines(unknown, [message], 'Asia/Shanghai')
    assert 'event_time' in TRIAGE_PROMPT and '无工具权限' in TRIAGE_PROMPT


@pytest.mark.parametrize('method,schema', [('summarize', 'SummaryData'), ('parse_config', 'ConfigIntent'),
    ('parse_preferences', 'PreferenceIntent'), ('triage', 'TriageResult')])
@pytest.mark.parametrize('raw', ['private-chat-secret INVALID', '{"secret":"personal-preference-secret"}'])
async def test_all_llm_schemas_never_log_raw_output(method, schema, raw, caplog):
    client, requests = mock_client([completion(raw)], DeepSeekConfig(retries=0))
    try:
        with pytest.raises(RetryableModelOutputError):
            if method == 'triage':
                await client.triage('private-prompt-secret')
            else:
                await getattr(client, method)('private-prompt-secret', AsyncMock())
        assert schema in caplog.text and f'response_length={len(raw)}' in caplog.text
        assert raw not in caplog.text
        assert 'private-chat-secret' not in caplog.text and 'personal-preference-secret' not in caplog.text
        assert 'private-prompt-secret' not in caplog.text
        assert 'tools' not in requests[0]
    finally:
        await client.close()


async def test_triage_sdk_does_not_multiply_persistent_retry_budget():
    client, requests = mock_client([completion('')], DeepSeekConfig(retries=5))
    try:
        with pytest.raises(RetryableModelOutputError):
            await client.triage('x')
        assert len(requests) == 1
        assert requests[0]['thinking'] == {'type': 'disabled'}
    finally:
        await client.close()


async def test_triage_sdk_returns_strict_multi_item_json():
    expected = TriageResult(items=[item([1]), item([2], category='schedule')], ignored_message_ids=[3])
    client, _ = mock_client([completion(expected.model_dump_json())])
    try:
        result = await client.triage(json.dumps({'message_ids': [1, 2, 3]}))
        assert result == expected
    finally:
        await client.close()
