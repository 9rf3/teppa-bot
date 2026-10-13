"""Мелкие помощники: суммы, даты, QR-токен, дедлайн удержания."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

# Месяцы для «24 октября 2026 г.» — как на сайте.
_MONTHS = [
    'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
]


def money(amount: int) -> str:
    """200990 -> '200 990 сум'."""
    return f'{amount:,}'.replace(',', ' ') + ' сум'


def to_tiyin(amount_uzs: int) -> int:
    """Payme считает в тийинах: 1 сум = 100 тийинов."""
    return int(round(amount_uzs * 100))


def match_amount(received: str | int | None, expected_uzs: int) -> int | None:
    """Click шлёт сумму в сумах или тийинах — сверяем оба варианта."""
    if received is None or received == '':
        return None
    try:
        value = float(str(received).replace(',', '.'))
    except ValueError:
        return None
    for candidate in (expected_uzs, to_tiyin(expected_uzs)):
        if abs(value - candidate) < 0.001:
            return candidate
    return None


def fmt_date(value: str) -> str:
    """'2026-10-24' -> '24 октября 2026 г.'"""
    try:
        parsed = datetime.strptime(value[:10], '%Y-%m-%d')
    except ValueError:
        return value
    return f'{parsed.day} {_MONTHS[parsed.month - 1]} {parsed.year} г.'


def fmt_short(value: str) -> str:
    """'2026-10-24' -> '24.10'"""
    try:
        parsed = datetime.strptime(value[:10], '%Y-%m-%d')
    except ValueError:
        return value
    return parsed.strftime('%d.%m')


def parse_when(trip: dict) -> datetime | None:
    """Момент отправления рейса (локальное время Узбекистана не уточняем —
    сравниваем с датой рейса в UTC, как и сайт: date + time)."""
    raw_date = str(trip.get('date') or '')[:10]
    raw_time = str(trip.get('time') or '00:00').strip() or '00:00'
    try:
        parsed = datetime.strptime(f'{raw_date} {raw_time}', '%Y-%m-%d %H:%M')
    except ValueError:
        try:
            parsed = datetime.strptime(raw_date, '%Y-%m-%d')
        except ValueError:
            return None
    return parsed.replace(tzinfo=timezone.utc)


def has_departed(trip: dict) -> bool:
    when = parse_when(trip)
    return bool(when and when <= datetime.now(timezone.utc))


def create_booking_id() -> str:
    """Тот же вид, что у сайта: BLD-123456789."""
    return f'BLD-{str(int(datetime.now().timestamp() * 1000))[-9:]}'


def create_qr_token(booking_id: str) -> str:
    """Совместимо с сайтом: '<id>.<32 hex>' (app/lib/ticket.ts)."""
    return f'{booking_id}.{uuid.uuid4().hex}'


def hold_deadline(created_at: str, hold_minutes: int) -> datetime:
    """Дедлайн удержания: 12 минут с момента брони (как на сайте)."""
    try:
        created = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
    except ValueError:
        created = datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created + timedelta(minutes=hold_minutes)


def hold_left(deadline: datetime, now: datetime | None = None) -> timedelta:
    now = now or datetime.now(timezone.utc)
    return deadline - now


def tg_post_link(chat_id: int, message_id: int) -> str:
    """Ссылка на пост в супергруппе: https://t.me/c/<id>/<msg>. Для ЛС — пусто."""
    raw = str(chat_id)
    return f'https://t.me/c/{raw[4:]}/{message_id}' if raw.startswith('-100') else ''


def parse_tg_post(link: str) -> tuple[int, int] | None:
    """'https://t.me/c/123456/789' -> (-100123456, 789); не разобралось — None."""
    parts = (link or '').strip().rstrip('/').split('/')
    try:
        if len(parts) >= 3 and parts[-3] == 'c':
            return int('-100' + parts[-2]), int(parts[-1])
    except ValueError:
        return None
    return None


def seconds_since(iso: str) -> float:
    """Сколько секунд прошло с ISO-момента (плохое значение → 0)."""
    try:
        moment = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())


def mmss(delta: timedelta) -> str:
    """'07:58' — для таймера; меньше нуля не уходит."""
    total = max(0, int(delta.total_seconds()))
    return f'{total // 60:02d}:{total % 60:02d}'
