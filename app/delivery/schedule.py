from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def quiet_end(preferences, now: float, timezone: str) -> float | None:
    if not preferences.quiet_hours_enabled:
        return None
    local = datetime.fromtimestamp(now, ZoneInfo(timezone))
    clock = local.strftime('%H:%M')
    start, end = preferences.quiet_start, preferences.quiet_end
    inside = start <= clock < end if start < end else clock >= start or clock < end
    if not inside:
        return None
    hour, minute = map(int, end.split(':'))
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += timedelta(days=1)
    return target.timestamp()


def slots(preferences, start: float, end: float, timezone: str) -> list[float]:
    zone = ZoneInfo(timezone)
    day = datetime.fromtimestamp(start, zone).replace(hour=0, minute=0, second=0, microsecond=0)
    last = datetime.fromtimestamp(end, zone)
    found = []
    while day.date() <= last.date():
        for clock in preferences.digest_times:
            hour, minute = map(int, clock.split(':'))
            value = day.replace(hour=hour, minute=minute).timestamp()
            if start < value <= end:
                found.append(value)
        day += timedelta(days=1)
    return sorted(set(found))


def next_digest(preferences, now: float, timezone: str) -> float | None:
    if not preferences.enabled or not preferences.digest_enabled:
        return None
    return next(iter(slots(preferences, now, now+2*86400, timezone)), None)
