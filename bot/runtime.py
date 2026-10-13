"""Общее состояние процесса: бот, чаты покупателей, задачи удержания."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # noqa: F401
    from aiogram import Bot

log = logging.getLogger('teppa')

bot: 'Bot | None' = None
settings = None
db = None
payments = None  # bot.payments.service.PaymentService

# booking_id -> chat_id, чтобы прислать «билет готов», даже если платёж
# пришёл вебхуком от Payme/Click. Связка живёт в памяти: платёж идёт минуты.
_chats: dict[str, int] = {}


def bind(bot_instance: 'Bot', settings_obj, db_obj, payments_obj=None) -> None:
    global bot, settings, db, payments
    bot = bot_instance
    settings = settings_obj
    db = db_obj
    payments = payments_obj


def remember_chat(booking_id: str, chat_id: int) -> None:
    _chats[booking_id] = chat_id


def chat_for(booking_id: str) -> int | None:
    return _chats.get(booking_id)


def forget(booking_id: str) -> None:
    _chats.pop(booking_id, None)


# Задачи, снимающие место, если за 12 минут не заплатили.
_holds: dict[str, asyncio.Task] = {}


def cancel_hold_task(booking_id: str) -> None:
    task = _holds.pop(booking_id, None)
    if task and not task.done():
        task.cancel()


def set_hold_task(booking_id: str, task: asyncio.Task) -> None:
    old = _holds.pop(booking_id, None)
    if old and not old.done():
        old.cancel()
    _holds[booking_id] = task


async def shutdown() -> None:
    """Корректно закрываем HTTP-клиент Supabase при остановке."""
    if payments is not None and hasattr(payments, 'stop'):
        payments.stop()  # гасим таймеры авто-подтверждения чеков
    for task in list(_holds.values()):
        if not task.done():
            task.cancel()
    _holds.clear()
    if db is not None:
        await db.close()
