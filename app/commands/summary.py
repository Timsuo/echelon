import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def parse_window(argument: str, now: datetime, timezone: str) -> tuple[float, float]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(ZoneInfo(timezone))
    if argument == "today":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        match = re.fullmatch(r"([1-9]\d{0,3})([mh])", argument)
        if not match:
            raise ValueError("用法：/summary 2h、/summary 30m 或 /summary today")
        minutes = int(match[1]) * (60 if match[2] == "h" else 1)
        if minutes > 7 * 24 * 60:
            raise ValueError("单次窗口最多 7 天")
        start = now - timedelta(minutes=minutes)
    return start.timestamp(), now.timestamp()
