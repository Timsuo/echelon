import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.triage.models import TriageResult


def validate_deadlines(result: TriageResult, messages: list[dict], timezone: str) -> None:
    """Reject demonstrably wrong relative dates; ambiguity must remain null, never use wall clock."""
    by_id = {message['id']: message for message in messages}
    zone = ZoneInfo(timezone)
    for item in result.items:
        if item.deadline_at is None:
            continue
        text = item.deadline_text or ''
        match = re.search(r'大后天|后天|明天|今天|下周[一二三四五六日天]', text)
        if not match:
            continue
        token = match[0]
        sources = [by_id[i] for i in item.source_message_ids if token in by_id[i]['normalized_text']]
        dates = set()
        for source in sources:
            date = datetime.fromtimestamp(source['event_time'], zone).date()
            if token.startswith('下周'):
                weekday = '一二三四五六日'.index(token[-1].replace('天', '日'))
                dates.add(date + timedelta(days=7 - date.weekday() + weekday))
            else:
                dates.add(date + timedelta(days={'今天': 0, '明天': 1, '后天': 2, '大后天': 3}[token]))
        if len(dates) != 1 or datetime.fromisoformat(item.deadline_at).astimezone(zone).date() not in dates:
            raise ValueError('Relative deadline does not match original event_time; use null when ambiguous')
