"""Рассылки: билет с QR-кодом покупателю, уведомления администраторам."""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

import qrcode
from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

from . import keyboards, runtime, texts
from .utils import fmt_date, money

log = logging.getLogger('teppa.notify')


def qr_png(token: str) -> BufferedInputFile:
    """QR-код билета: строка токена — ровно то, что кодирует сайт."""
    image = qrcode.make(token, box_size=8, border=2)
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    buffer.seek(0)
    return BufferedInputFile(buffer.read(), filename='ticket.png')


def ticket_text(booking: dict[str, Any], trip: dict[str, Any] | None = None) -> str:
    title = booking.get('trip_title') or booking.get('direction') or 'Поездка'
    lines = [
        texts.TICKET_HEADER,
        title,
    ]
    if trip:
        lines.append(f"Дата: {fmt_date(str(trip.get('date')))} · {trip.get('time')}")
    lines += [
        f"Место: №{booking.get('seat')}",
        f"Пассажир: {(booking.get('first_name') or '') + ' ' + (booking.get('last_name') or '')}".strip(),
        f"Оплачено: {money(int(booking.get('price') or 0))}",
        f"Бронь: {booking.get('id')}",
        '',
        texts.PAID_BADGE,
    ]
    return '\n'.join(lines)


async def send_ticket(bot: Bot, chat_id: int, booking: dict[str, Any],
                      trip: dict[str, Any] | None = None) -> None:
    """Билет: сообщение + QR-фото. Падения почты не должны ронять оплату."""
    try:
        await bot.send_message(chat_id, ticket_text(booking, trip))
    except Exception:  # noqa: BLE001
        log.exception('не удалось отправить билет chat=%s', chat_id)
        return

    token = booking.get('qr_token')
    if not token:
        return
    try:
        await bot.send_photo(
            chat_id,
            qr_png(str(token)),
            caption=texts.TICKET_QR_CAPTION,
            reply_markup=keyboards.ticket_menu(str(booking.get('id'))),
        )
    except Exception:  # noqa: BLE001
        log.exception('не удалось отправить QR chat=%s', chat_id)


async def notify_user(booking_id: str, text: str,
                      reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    """Пишем покупателю, если знаем его чат (платёж оформлен в этом процессе)."""
    chat_id = runtime.chat_for(booking_id)
    bot = runtime.bot
    if not chat_id or not bot:
        return False
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
        return True
    except Exception:  # noqa: BLE001
        log.exception('не удалось написать покупателю chat=%s', chat_id)
        return False


async def notify_admins(text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    bot = runtime.bot
    settings = runtime.settings
    if not bot or not settings:
        return
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception:  # noqa: BLE001
            log.exception('не удалось написать админу %s', admin_id)
