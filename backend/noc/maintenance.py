from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def active_window(window, now: datetime | None = None) -> bool:
    if not window['enabled']:
        return False
    now = now or datetime.now(timezone.utc)
    start, end = window['starts_at'], window['ends_at']
    if window['repeat'] == 'none':
        return start <= now < end
    zone = ZoneInfo(window['timezone'])
    local_start, local_now = start.astimezone(zone), now.astimezone(zone)
    if now < start:
        return False
    day = local_now.date()
    if window['repeat'] == 'weekly':
        day -= timedelta(days=(day.weekday() - local_start.weekday()) % 7)
    occurrence = datetime.combine(day, local_start.timetz(), zone)
    if occurrence > local_now:
        occurrence -= timedelta(days=7 if window['repeat'] == 'weekly' else 1)
    return occurrence.astimezone(timezone.utc) <= now < occurrence.astimezone(timezone.utc) + (end - start)