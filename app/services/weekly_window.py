"""
Моменты и окна еженедельной подборки (чистые функции без БД и сети).

Подборка уходит по пятницам в ``WEEKLY_DIGEST_TIME`` (``WEEKLY_DIGEST_TZ``). Окна
считаются по *запланированным* моментам, а не по фактическому времени отправки:
при отложенной отправке и догоне соседние окна не пересекаются и не имеют разрывов.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

# Отложенная отправка и догон — только в дневное окно по местному времени
DAY_START = time(9, 0)
DAY_END = time(22, 0)
# Последний час воскресенья: уходит то, что уже оценено, неоценённые исключаются
SUNDAY_LAST_HOUR_START = time(21, 0)

_FRIDAY = 4  # datetime.weekday(): понедельник = 0
_PERIOD = timedelta(days=7)


def last_digest_moment(now: datetime, send_time: time, tz: ZoneInfo) -> datetime:
    """Последний прошедший запланированный момент рассылки (пятница ``send_time``) в ``tz``."""
    local = now.astimezone(tz)
    friday = local.date() - timedelta(days=(local.weekday() - _FRIDAY) % 7)
    moment = datetime.combine(friday, send_time, tzinfo=tz)
    if moment > local:
        moment -= _PERIOD
    return moment


def previous_digest_moment(moment: datetime) -> datetime:
    """Предыдущий запланированный момент (начало окна подборки за ``moment``)."""
    return moment - _PERIOD


def sunday_at(moment: datetime, at: time) -> datetime:
    """Воскресенье той же недели, что и пятничный ``moment``, в время ``at``."""
    return datetime.combine(moment.date() + timedelta(days=2), at, tzinfo=moment.tzinfo)


def in_daytime(now: datetime, tz: ZoneInfo) -> bool:
    """Сейчас дневное окно отправки (09:00–22:00 по ``tz``)."""
    return DAY_START <= now.astimezone(tz).time() < DAY_END
