from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.commands.summary import parse_window
from app.llm.schemas import SummaryData
from app.onebot.normalizer import normalize


def test_normalizer_segments():
    result = normalize([
        {"type": "text", "data": {"text": "hi"}},
        {"type": "at", "data": {"qq": "99"}},
        {"type": "reply", "data": {"id": 123}},
        {"type": "image", "data": {}},
        {"type": "file", "data": {"name": "a.txt"}},
        {"type": "record", "data": {}},
    ])
    assert result.text == "hi[@99][回复 message_id=123][图片][文件: a.txt][unsupported:record]"
    assert result.reply_to_message_id == "123"


def test_normalizer_cq():
    result = normalize("a&#91;b&#93;&amp;[CQ:reply,id=-3][CQ:file,name=a&#44;b.txt]")
    assert result.text == "a[b]&[回复 message_id=-3][文件: a,b.txt]"
    assert result.reply_to_message_id == "-3"


@pytest.mark.parametrize(("argument", "seconds"), [("2h", 7200), ("30m", 1800), ("today", 10 * 3600)])
def test_window(argument, seconds):
    now = datetime(2026, 9, 30, 2, tzinfo=UTC)
    start, end = parse_window(argument, now, "Asia/Shanghai")
    assert end - start == seconds


@pytest.mark.parametrize("argument", ["", "0h", "-2h", "8d", "9999h", "2h junk"])
def test_invalid_window(argument):
    with pytest.raises(ValueError):
        parse_window(argument, datetime.now(UTC), "Asia/Shanghai")


def test_schema_validation():
    valid = SummaryData.empty().model_dump_json()
    assert SummaryData.model_validate_json(valid).topics == []
    for text in ['{}', 'not json', '{"topics": "bad"}', valid[:-1] + ',"action":"send_group_msg"}']:
        with pytest.raises(ValidationError):
            SummaryData.model_validate_json(text)
