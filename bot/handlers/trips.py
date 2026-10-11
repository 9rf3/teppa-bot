"""Список рейсов и карточка поездки (те же правила, что на сайте:
опубликовано и ещё не ушло)."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import keyboards, runtime, texts
from ..utils import fmt_date, fmt_short, has_departed, money

router = Router()


async def upcoming() -> list[dict[str, Any]]:
    """Ближайшие открытые рейсы без учёта уже ушедших."""
    trips = await runtime.db.upcoming_trips()
    return [trip for trip in trips if not has_departed(trip)]


def trip_card(trip: dict[str, Any]) -> str:
    fee = runtime.settings.service_fee if runtime.settings else 0
    return '\n'.join(
        [
            f"🚍 {trip.get('title') or trip.get('direction') or 'Рейс'}",
            str(trip.get('direction') or ''),
            f"📅 {fmt_date(str(trip.get('date')))} · {trip.get('time')}",
            f"💸 {money(int(trip.get('price') or 0) + fee)} — сервисный сбор включён",
        ]
    ).strip()


def _list_text(trips: list[dict[str, Any]], db_free: dict[str, int]) -> str:
    if not trips:
        return texts.NO_TRIPS
    lines = [texts.TRIPS_HEADER, '']
    for trip in trips:
        free = db_free.get(str(trip['id']))
        free_text = f" · свободно {free}" if free is not None else ''
        lines.append(
            f"• {trip.get('title') or trip.get('direction')} — "
            f"{fmt_short(str(trip.get('date')))} {trip.get('time')} — "
            f"{money(int(trip.get('price') or 0))}{free_text}"
        )
    return '\n'.join(lines)


async def _free_seats(trips: list[dict[str, Any]]) -> dict[str, int]:
    """Свободные места по каждому рейсу — как на сетке сайта."""
    free: dict[str, int] = {}
    for trip in trips:
        trip_id = str(trip['id'])
        try:
            rows = await runtime.db.bookings_by_trip(trip_id)
            taken = {int(row['seat']) for row in rows if row.get('seat')}
            blocked = {int(x) for x in (trip.get('blocked_seats') or [])}
            free[trip_id] = max(0, int(trip.get('seats') or 0) - len(taken | blocked))
        except Exception:  # noqa: BLE001 — счётчик не должен ломать список
            continue
    return free


async def send_list(message: Message) -> None:
    trips = await upcoming()
    await message.answer(
        _list_text(trips, await _free_seats(trips)) if trips else texts.NO_TRIPS,
        reply_markup=keyboards.trips_list(trips) if trips else None,
    )


async def _edit_list(target: Message, trips: list[dict[str, Any]], kb: InlineKeyboardMarkup) -> None:
    text = _list_text(trips, await _free_seats(trips))
    try:
        await target.edit_text(text, reply_markup=kb)
    except Exception:  # noqa: BLE001 — «message is not modified» и прочее
        await target.answer(text, reply_markup=kb)


@router.message(F.text == '🚍 Рейсы')
async def on_trips_button(message: Message) -> None:
    await send_list(message)


@router.callback_query(F.data == 'trips:list')
async def on_trips_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    trips = await upcoming()
    await _edit_list(callback.message, trips, keyboards.trips_list(trips))


@router.callback_query(F.data.startswith('trip:'))
async def on_trip(callback: CallbackQuery) -> None:
    trip_id = callback.data.split(':', 1)[1]
    try:
        trip = await runtime.db.trip(trip_id)
    except Exception:  # noqa: BLE001
        trip = None
    await callback.answer()
    if not trip or trip.get('status') != 'published' or has_departed(trip):
        await callback.message.answer(texts.NO_TRIPS)
        return

    free = (await _free_seats([trip])).get(trip_id)
    text = trip_card(trip)
    if free is not None:
        text += f"\n🪑 Свободно {free} из {trip.get('seats')} мест"
    try:
        await callback.message.edit_text(text, reply_markup=keyboards.trip_info(trip_id))
    except Exception:  # noqa: BLE001
        await callback.message.answer(text, reply_markup=keyboards.trip_info(trip_id))
