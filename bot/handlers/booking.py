"""Бронирование: место → пассажир → телефон → email → обед → подтверждение."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards, notify, runtime, texts
from ..db import SeatConflict, SupabaseError
from ..utils import create_booking_id, fmt_date, money
from . import payment

router = Router()


class Bk(StatesGroup):
    """Этапы заполнения формы брони."""

    seat = State()
    passenger = State()
    phone = State()
    email = State()
    lunch = State()
    confirm = State()


# ---------- Схема мест ----------


async def _seat_context(trip: dict) -> tuple[set[int], set[int], int]:
    rows = await runtime.db.bookings_by_trip(str(trip['id']))
    taken = {int(row['seat']) for row in rows if row.get('seat')}
    blocked = {int(x) for x in (trip.get('blocked_seats') or [])}
    free = max(0, int(trip.get('seats') or 0) - len(taken | blocked))
    return taken, blocked, free


async def _send_grid(message: Message, trip: dict, *, edit: bool) -> None:
    taken, blocked, free = await _seat_context(trip)
    text = (
        f"{trip.get('title') or trip.get('direction')}\n"
        f"📅 {fmt_date(str(trip.get('date')))} · {trip.get('time')}\n\n"
        f"{texts.PICK_SEAT}\n"
        f"Свободно {free} из {trip.get('seats')} мест"
    )
    kb = keyboards.seat_grid(str(trip['id']), int(trip.get('seats') or 20), taken, blocked)
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:  # noqa: BLE001 — текст не изменился
            pass
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith('book:'))
async def on_pick_seat(callback: CallbackQuery, state: FSMContext) -> None:
    trip_id = callback.data.split(':', 1)[1]
    try:
        trip = await runtime.db.trip(trip_id)
    except SupabaseError:
        trip = None
    if not trip or trip.get('status') != 'published':
        await callback.answer(texts.NO_TRIPS, show_alert=True)
        return

    await state.set_state(Bk.seat)
    await state.update_data(trip=trip, seat=None)
    await callback.answer()
    await _send_grid(callback.message, trip, edit=True)


@router.callback_query(Bk.seat, F.data.startswith('seat:'))
async def on_seat(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(':')
    trip_id, seat_no = parts[1], int(parts[2])

    data = await state.get_data()
    trip = data.get('trip') or {}
    if str(trip.get('id')) != trip_id:
        try:
            trip = await runtime.db.trip(trip_id) or {}
        except SupabaseError:
            trip = {}
        await state.update_data(trip=trip)

    taken, blocked, _ = await _seat_context(trip)
    if seat_no in blocked:
        await callback.answer(texts.SEAT_BLOCKED, show_alert=True)
        return
    if seat_no in taken:
        await callback.answer(texts.SEAT_BUSY, show_alert=True)
        return

    await state.update_data(seat=seat_no)
    await state.set_state(Bk.passenger)
    await callback.answer()

    name = (callback.from_user.full_name or '').strip() or 'Я пассажир'
    await callback.message.answer(
        texts.ASK_PASSENGER, reply_markup=keyboards.plain_menu(name[:64], '❌ Отмена')
    )


# ---------- Пассажир ----------


def _split_name(raw: str) -> tuple[str, str]:
    parts = raw.split()
    if not parts:
        return '', ''
    return parts[0][:64], ' '.join(parts[1:])[:64]


@router.message(Bk.passenger, F.text)
async def on_passenger(message: Message, state: FSMContext) -> None:
    first, last = _split_name(message.text.strip())
    if not first:
        await message.answer(texts.ASK_PASSENGER)
        return
    await state.update_data(first_name=first, last_name=last)
    await state.set_state(Bk.phone)
    await message.answer(texts.ASK_PHONE, reply_markup=keyboards.phone_menu())


# ---------- Телефон ----------


def _valid_phone(raw: str) -> str | None:
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if len(digits) < 9:
        return None
    return ('+' if raw.strip().startswith('+') else '') + digits[-12:]


async def _after_phone(message: Message, state: FSMContext, phone: str) -> None:
    await state.update_data(phone=phone)
    await state.set_state(Bk.email)
    await message.answer(texts.ASK_EMAIL, reply_markup=keyboards.skip_email())


@router.message(Bk.phone, F.contact)
async def on_contact(message: Message, state: FSMContext) -> None:
    phone = _valid_phone(message.contact.phone_number or '')
    if not phone:
        await message.answer(texts.PHONE_INVALID, reply_markup=keyboards.phone_menu())
        return
    await _after_phone(message, state, phone)


@router.message(Bk.phone, F.text)
async def on_phone(message: Message, state: FSMContext) -> None:
    phone = _valid_phone(message.text.strip())
    if not phone:
        await message.answer(texts.PHONE_INVALID, reply_markup=keyboards.phone_menu())
        return
    await _after_phone(message, state, phone)


# ---------- Email (необязательно, но связывает бронь с кабинетом сайта) ----------


async def _ask_lunch(message: Message, state: FSMContext) -> None:
    await state.set_state(Bk.lunch)
    settings = runtime.settings
    await message.answer(
        texts.ASK_LUNCH,
        reply_markup=keyboards.lunch(settings.lunch_price if settings else 100_000),
    )


@router.message(Bk.email, F.text)
async def on_email(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text in ('Пропустить', '/skip', '/start'):
        await state.update_data(email='')
        await _ask_lunch(message, state)
        return
    if '@' not in text or len(text) > 120:
        await message.answer('Похоже, это не email. Оставьте пусто — нажмите «Пропустить».',
                             reply_markup=keyboards.skip_email())
        return
    await state.update_data(email=text[:120])
    await _ask_lunch(message, state)


@router.callback_query(Bk.email, F.data == 'mail:skip')
async def on_email_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(email='')
    await state.set_state(Bk.lunch)
    await callback.answer()
    settings = runtime.settings
    await callback.message.answer(
        texts.ASK_LUNCH,
        reply_markup=keyboards.lunch(settings.lunch_price if settings else 100_000),
    )


# ---------- Обед и подтверждение ----------


def _total(trip: dict, lunch: bool) -> int:
    settings = runtime.settings
    fee = settings.service_fee if settings else 990
    lunch_price = (settings.lunch_price if settings else 100_000) if lunch else 0
    return max(0, int(trip.get('price') or 0)) + fee + lunch_price


async def _confirm_text(state: FSMContext) -> str:
    data = await state.get_data()
    trip = data.get('trip') or {}
    lunch = bool(data.get('lunch'))
    lines = [
        texts.CONFIRM_HEADER,
        '',
        f"{trip.get('title') or trip.get('direction')}",
        f"📅 {fmt_date(str(trip.get('date')))} · {trip.get('time')}",
        f"💺 Место №{data.get('seat')}",
        f"👤 {data.get('first_name', '')} {data.get('last_name', '')}".strip(),
        f"📱 {data.get('phone', '')}",
    ]
    if data.get('email'):
        lines.append(f"✉️ {data.get('email')}")
    lines.append(f"🍽 Обед: {'да' if lunch else 'нет'}")
    lines.append('')
    if lunch:
        settings = runtime.settings
        lines.append(f"Билет: {money(int(trip.get('price') or 0))}")
        lines.append(f"Сервисный сбор: {money(settings.service_fee if settings else 990)}")
        lines.append(f"Обед: {money(settings.lunch_price if settings else 100_000)}")
    lines.append(f"К оплате: {money(_total(trip, lunch))}")
    lines.append('')
    # До создания брони дедлайн ещё не зафиксирован — показываем полное окно.
    lines.append(texts.HOLD_LEFT.format(time=f'{settings_hold_minutes():02d}:00'))
    return '\n'.join(lines)


def settings_hold_minutes() -> int:
    return runtime.settings.hold_minutes if runtime.settings else 12


@router.callback_query(Bk.lunch, F.data.startswith('lunch:'))
async def on_lunch(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(lunch=callback.data.split(':', 1)[1] == '1')
    await state.set_state(Bk.confirm)
    await callback.answer()
    text = await _confirm_text(state)
    try:
        await callback.message.edit_text(text, reply_markup=keyboards.confirm())
    except Exception:  # noqa: BLE001
        await callback.message.answer(text, reply_markup=keyboards.confirm())


@router.callback_query(Bk.confirm, F.data == 'bk:cancel')
async def on_decline(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer(texts.CANCELLED_BY_USER)
    await callback.message.answer(texts.CANCELLED_BY_USER, reply_markup=keyboards.main_menu())


@router.callback_query(Bk.confirm, F.data == 'bk:ok')
async def on_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    trip = data.get('trip') or {}
    seat = data.get('seat')
    if not trip.get('id') or not seat:
        await state.clear()
        await callback.answer(texts.CANCELLED_BY_USER, show_alert=True)
        return

    lunch = bool(data.get('lunch'))
    booking_id = create_booking_id()
    payload = {
        'id': booking_id,
        'user_id': None,
        'trip_id': str(trip['id']),
        'trip_title': str(trip.get('title') or ''),
        'direction': str(trip.get('direction') or ''),
        'seat': int(seat),
        'price': _total(trip, lunch),
        'first_name': str(data.get('first_name') or ''),
        'last_name': str(data.get('last_name') or ''),
        'phone': str(data.get('phone') or ''),
        'email': str(data.get('email') or ''),
        'status': 'Новая',
        'lunch': lunch,
        'comment': 'Telegram-бот',
    }

    try:
        booking = await runtime.db.create_booking(payload)
    except SeatConflict:
        await callback.answer(texts.SEAT_TAKEN, show_alert=True)
        await state.set_state(Bk.seat)
        await _send_grid(callback.message, trip, edit=True)
        return
    except SupabaseError:
        await callback.answer('Не удалось создать бронь. Попробуйте ещё раз.', show_alert=True)
        return

    await state.clear()
    await callback.answer('Бронь создана ✓')

    runtime.remember_chat(booking_id, callback.message.chat.id)
    runtime.payments.start_hold(booking)

    await notify.notify_admins(
        texts.ADMIN_NEW_BOOKING.format(
            booking=booking_id,
            title=booking.get('trip_title') or booking.get('direction') or '',
            seat=booking.get('seat'),
            passenger=f"{payload['first_name']} {payload['last_name']}".strip(),
            phone=payload['phone'],
            amount=money(payload['price']),
        )
    )

    await payment.send_menu(callback.message, booking_id)
