"""Клавиатуры бота: главное меню, схема мест, оплата, админка."""

from __future__ import annotations

from typing import Any, Iterable

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from . import texts

# ---------- Reply-клавиатура (главное меню) ----------


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text='🚍 Рейсы')],
            [KeyboardButton(text='🎫 Мои брони'), KeyboardButton(text='ℹ️ Помощь')],
        ],
        resize_keyboard=True,
        input_field_placeholder='Нажмите кнопку ниже',
    )


def phone_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text='📱 Отправить контакт', request_contact=True)],
            [KeyboardButton(text='❌ Отмена')],
        ],
        resize_keyboard=True,
        input_field_placeholder='+998901234567',
    )


def plain_menu(*labels: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label)] for label in labels],
        resize_keyboard=True,
    )


# ---------- Inline-клавиатуры ----------


def _inline(rows: Iterable[Iterable[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows])


def trips_list(trips: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    return _inline(
        [
            [
                InlineKeyboardButton(
                    text=f"{trip['title']} · {trip.get('date', '')[8:10]}.{trip.get('date', '')[5:7]}",
                    callback_data=f"trip:{trip['id']}",
                )
            ]
            for trip in trips
        ]
    )


def trip_info(trip_id: str) -> InlineKeyboardMarkup:
    return _inline(
        [
            [InlineKeyboardButton(text='Выбрать место →', callback_data=f'book:{trip_id}')],
            [InlineKeyboardButton(text='← Ко всем рейсам', callback_data='trips:list')],
        ]
    )


def seat_grid(
    trip_id: str,
    seats: int,
    taken: set[int],
    blocked: set[int],
    per_row: int = 6,
    max_rows: int = 8,
) -> InlineKeyboardMarkup:
    """Схема автобуса: занятые и заблокированные места — «призраки»."""
    rows: list[list[InlineKeyboardButton]] = []
    for start in range(0, min(seats, per_row * max_rows), per_row):
        row: list[InlineKeyboardButton] = []
        for n in range(start + 1, min(start + per_row + 1, seats + 1)):
            if n in taken or n in blocked:
                row.append(InlineKeyboardButton(text='・', callback_data=f'seat:{trip_id}:{n}'))
            else:
                row.append(InlineKeyboardButton(text=str(n), callback_data=f'seat:{trip_id}:{n}'))
        rows.append(row)
    rows.append([InlineKeyboardButton(text='← К рейсам', callback_data='trips:list')])
    return _inline(rows)


def lunch(lunch_price: int) -> InlineKeyboardMarkup:
    return _inline(
        [
            [
                InlineKeyboardButton(text=f'Обед +{lunch_price // 1000}k', callback_data='lunch:1'),
                InlineKeyboardButton(text='Без обеда', callback_data='lunch:0'),
            ]
        ]
    )


def confirm() -> InlineKeyboardMarkup:
    return _inline(
        [
            [InlineKeyboardButton(text=texts.CONFIRM, callback_data='bk:ok')],
            [InlineKeyboardButton(text=texts.CANCEL, callback_data='bk:cancel')],
        ]
    )


def payment_methods(booking_id: str, *, payme: bool, click: bool, card: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if payme:
        rows.append([InlineKeyboardButton(text='🟡 Payme', callback_data=f'pay:{booking_id}:payme')])
    if click:
        rows.append([InlineKeyboardButton(text='💳 Click', callback_data=f'pay:{booking_id}:click')])
    if card:
        rows.append([InlineKeyboardButton(text='🏦 Перевод на карту', callback_data=f'pay:{booking_id}:card')])
    rows.append([InlineKeyboardButton(text='🔄 Проверить оплату', callback_data=f'check:{booking_id}')])
    rows.append([InlineKeyboardButton(text='❌ Отменить бронь', callback_data=f'cancel:{booking_id}')])
    return _inline(rows)


def back_to_booking(booking_id: str) -> InlineKeyboardMarkup:
    return _inline(
        [[InlineKeyboardButton(text='← Способы оплаты', callback_data=f'menu:{booking_id}')]]
    )


def ticket_menu(booking_id: str) -> InlineKeyboardMarkup:
    return _inline(
        [
            [InlineKeyboardButton(text='🚍 Ещё рейсы', callback_data='trips:list')],
            [InlineKeyboardButton(text='🎫 Мои брони', callback_data='mine:list')],
        ]
    )


def skip_email() -> InlineKeyboardMarkup:
    return _inline([[InlineKeyboardButton(text='Пропустить', callback_data='mail:skip')]])


def my_bookings(bookings: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    return _inline(
        [
            [
                InlineKeyboardButton(
                    text=f"{row.get('trip_title') or row.get('direction', '')} · №{row.get('seat')}",
                    callback_data=f"check:{row['id']}",
                )
            ]
            for row in bookings
        ]
    )


# ---------- Админка ----------


def admin_panel(pending: int) -> InlineKeyboardMarkup:
    return _inline(
        [
            [InlineKeyboardButton(text=f'💳 Оплаты на подтверждение ({pending})', callback_data='adm:pending')],
            [InlineKeyboardButton(text='📊 Статистика', callback_data='adm:stats')],
        ]
    )


def admin_pending(payments: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for payment in payments:
        booking_id = payment['booking_id']
        rows.append([InlineKeyboardButton(text=f'— {booking_id} —', callback_data=f'adm:info:{booking_id}')])
        rows.append(
            [
                InlineKeyboardButton(text=f'{texts.APPROVE} {booking_id}', callback_data=f'adm:ok:{booking_id}'),
                InlineKeyboardButton(text=texts.REJECT, callback_data=f'adm:no:{booking_id}'),
            ]
        )
    rows.append([InlineKeyboardButton(text=texts.BACK, callback_data='adm:menu')])
    return _inline(rows)


def admin_back() -> InlineKeyboardMarkup:
    return _inline([[InlineKeyboardButton(text=texts.BACK, callback_data='adm:menu')]])


def receipt_actions(booking_id: str) -> InlineKeyboardMarkup:
    """Кнопки подтверждения чека, прикреплённые к пересланному фото."""
    return _inline(
        [
            [
                InlineKeyboardButton(text=f'{texts.APPROVE} {booking_id}',
                                     callback_data=f'adm:ok:{booking_id}'),
                InlineKeyboardButton(text=texts.REJECT, callback_data=f'adm:no:{booking_id}'),
            ]
        ]
    )
