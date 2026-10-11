"""Админка: подтверждение переводов на карту, статистика, уведомления."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import keyboards, runtime, texts
from ..db import SupabaseError
from ..utils import money

log = logging.getLogger('teppa.admin')

router = Router()


def _is_admin(user_id: int | None) -> bool:
    settings = runtime.settings
    return bool(settings and user_id in settings.admin_ids)


async def _pending() -> list[dict]:
    try:
        return await runtime.db.pending_card_payments()
    except SupabaseError:
        log.exception('не удалось получить неподтверждённые оплаты')
        return []


async def _open_panel(target, *, edit: bool = False) -> None:  # noqa: ANN001
    pending = await _pending()
    text = 'Панель TEPPA — выбор действия:'
    kb = keyboards.admin_panel(len(pending))
    if edit:
        try:
            await target.edit_text(text, reply_markup=kb)
            return
        except Exception:  # noqa: BLE001
            pass
    await target.answer(text, reply_markup=kb)


@router.message(Command('admin'))
async def on_admin(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    await _open_panel(message)


@router.callback_query(F.data.startswith('adm:'))
async def on_admin_callback(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return

    parts = callback.data.split(':')
    action = parts[1]

    if action == 'menu':
        await callback.answer()
        await _open_panel(callback.message, edit=True)
        return

    if action == 'pending':
        await callback.answer()
        payments = await _pending()
        if not payments:
            await callback.answer(texts.ADMIN_PENDING_EMPTY, show_alert=True)
            return
        lines = [texts.ADMIN_PENDING, '']
        for payment in payments:
            booking = None
            try:
                booking = await runtime.db.booking(str(payment['booking_id']))
            except SupabaseError:
                pass
            lines.append(
                f"• {payment['booking_id']} — {money(int(payment.get('amount') or 0))}\n"
                f"  {(booking or {}).get('trip_title', '')} · место №{(booking or {}).get('seat', '—')}"
            )
        try:
            await callback.message.edit_text('\n'.join(lines),
                                             reply_markup=keyboards.admin_pending(payments))
        except Exception:  # noqa: BLE001
            await callback.message.answer('\n'.join(lines),
                                          reply_markup=keyboards.admin_pending(payments))
        return

    if action == 'ok':
        booking_id = parts[2]
        await callback.answer()
        try:
            payment = await runtime.db.payment(booking_id)
            ok = await runtime.payments.approve_card(booking_id)
        except SupabaseError:
            await callback.answer('База недоступна', show_alert=True)
            return
        amount = money(int((payment or {}).get('amount') or 0))
        text = (texts.ADMIN_APPROVED.format(booking=booking_id, amount=amount) if ok
                else texts.ADMIN_REJECTED.format(booking=booking_id))
        await callback.message.answer(text, reply_markup=keyboards.admin_back())
        await _refresh_receipt(callback, booking_id, '✅ Оплата подтверждена')
        return

    if action == 'no':
        booking_id = parts[2]
        await callback.answer()
        try:
            await runtime.payments.reject_card(booking_id)
        except SupabaseError:
            await callback.answer('База недоступна', show_alert=True)
            return
        await callback.message.answer(texts.ADMIN_REJECTED.format(booking=booking_id),
                                      reply_markup=keyboards.admin_back())
        await _refresh_receipt(callback, booking_id, '❌ Чек отклонён')
        return

    if action == 'info':
        booking_id = parts[2]
        await callback.answer()
        try:
            booking = await runtime.db.booking(booking_id)
        except SupabaseError:
            booking = None
        if not booking:
            await callback.answer(texts.NO_BOOKING, show_alert=True)
            return
        text = (
            f"Бронь {booking_id}\n"
            f"{booking.get('trip_title')} · место №{booking.get('seat')}\n"
            f"Пассажир: {(booking.get('first_name') or '') + ' ' + (booking.get('last_name') or '')}\n"
            f"Телефон: {booking.get('phone')}\n"
            f"Статус: {booking.get('status')}\n"
            f"Сумма: {money(int(booking.get('price') or 0))}"
        )
        await callback.message.answer(text, reply_markup=keyboards.admin_back())
        return

    if action == 'stats':
        await callback.answer()
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        try:
            rows = await runtime.db.bookings_since(day_start)
            pending = await _pending()
        except SupabaseError:
            await callback.answer('База недоступна', show_alert=True)
            return
        paid_sum = sum(int(row.get('price') or 0) for row in rows if row.get('status') == 'Оплачена')
        await callback.message.answer(
            texts.ADMIN_STATS.format(bookings=len(rows), paid=money(paid_sum), pending=len(pending)),
            reply_markup=keyboards.admin_back(),
        )
        return

    await callback.answer()


async def _refresh_receipt(callback: CallbackQuery, booking_id: str, title: str) -> None:
    """Обновляет кнопки на пересланном фото чека (гасит «подтвердить/отклонить»)."""
    try:
        await callback.message.edit_caption(
            caption=f'{title}\nБронь {booking_id}',
            reply_markup=None,
        )
    except Exception:  # noqa: BLE001 — сообщение может быть не фото
        pass
