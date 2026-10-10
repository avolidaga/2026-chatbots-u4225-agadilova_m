"""Календарные сроки и быстрые напоминания; хранение времени — UTC."""

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from errors import InputError

UTC = timezone.utc


def local_timestamp(local: datetime, tz: ZoneInfo) -> float:
    aware = local.replace(tzinfo=tz)
    if aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != local:
        raise InputError("Такого местного времени нет из-за перевода часов.")
    if aware.utcoffset() != local.replace(tzinfo=tz, fold=1).utcoffset():
        raise InputError("Это время встречается дважды при переводе часов.")
    return aware.timestamp()


def parse_deadline(value: str, tz: ZoneInfo, now: datetime | None = None) -> tuple[float, bool]:
    now = now or datetime.now(UTC)
    value = value.strip()
    match = re.fullmatch(r"(\d{2}\.\d{2})(\.\d{4})?(?: (\d{2}:\d{2}))?", value)
    if not match:
        raise InputError("Введите ДД.ММ или ДД.ММ.ГГГГ; время ЧЧ:ММ необязательно.")
    date_part = match[1] + (match[2] or f".{now.astimezone(tz).year}")
    try:
        if match[3]:
            local = datetime.strptime(date_part + " " + match[3], "%d.%m.%Y %H:%M")
            return local_timestamp(local, tz), True
        chosen = datetime.strptime(date_part, "%d.%m.%Y").date()
        # Дата без времени действует до начала следующего местного дня, не 24 часа UTC.
        boundary = datetime.combine(chosen + timedelta(days=1), time())
        return local_timestamp(boundary, tz), False
    except (ValueError, OverflowError, OSError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("Некорректная дата или время.") from exc


def deadline_day(when: float, has_time: bool, tz: ZoneInfo):
    day = datetime.fromtimestamp(when, tz).date()
    return day if has_time else day - timedelta(days=1)


def quick_reminder(choice: str, tz: ZoneInfo, deadline: float | None = None,
                   now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    if choice == "15":
        result = now.timestamp() + 15 * 60
    elif choice == "60":
        result = now.timestamp() + 60 * 60
    elif choice == "tomorrow":
        tomorrow = now.astimezone(tz).date() + timedelta(days=1)
        result = local_timestamp(datetime.combine(tomorrow, time(9)), tz)
    elif choice == "before" and deadline is not None:
        result = deadline - 60 * 60
    else:
        raise InputError("Этот вариант напоминания недоступен.")
    if result <= now.timestamp():
        raise InputError("Время напоминания уже прошло. Выберите другое время.")
    return result
