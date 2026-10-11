"""Подключение всех роутеров обработчиков (порядок важен)."""

from __future__ import annotations

from aiogram import Dispatcher

from . import admin, booking, payment, start, trips


def register_all(dp: Dispatcher) -> None:
    # start первым: глубокие ссылки, меню и глобальная отмена должны
    # срабатывать раньше состояний бронирования.
    dp.include_router(start.router)
    dp.include_router(trips.router)
    dp.include_router(booking.router)
    dp.include_router(payment.router)
    dp.include_router(admin.router)
