"""Меню оплаты: Payme, Click, перевод на карту; проверка статуса; чек."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards, notify, runtime, texts
from ..db import SupabaseError
from ..payments import click as click_api
from ..payments import payme as payme_api
from ..utils import fmt_date, money

log = logging.getLogger('teppa.payment')

router = Router()


class Card(StatesGroup):
    """Ждём скриншот чека перевода на карту."""

    receipt = State()


# ---------- Меню ----------


def _menu_text(booking: dict, trip: dict | None) -> str:
    title = booking.get('trip_title') or booking.get('direction') or 'Поездка'
    lines = [
        texts.BOOKING_CREATED,
        '',
        f"{title} · место №{booking.get('seat')}",
    ]
    if trip:
        lines.append(f"📅 {fmt_date(str(trip.get('date')))} · {trip.get('time')}")
    lines.append(f"К оплате: {money(int(booking.get('price') or 0))}")
    lines.append('')
    lines.append(runtime.payments.hold_text(booking))
    return '\n'.join(lines)


async def _load(booking_id: str) -> tuple[dict | None, dict | None]:
    booking = await runtime.db.booking(booking_id)
    if not booking:
        return None, None
    trip = None
    try:
        trip = await runtime.db.trip(str(booking.get('trip_id') or ''))
    except SupabaseError:
        log.exception('рейс не загрузился: %s', booking.get('trip_id'))
    return booking, trip


async def send_menu(message: Message, booking_id: str) -> None:
    """Отправляет меню оплаты новым сообщением (после создания брони / со ссылки)."""
    try:
        booking, trip = await _load(booking_id)
    except SupabaseError:
        await message.answer(texts.NO_BOOKING)
        return
    if not booking:
        await message.answer(texts.NO_BOOKING)
        return
    if booking.get('status') == 'Оплачена':
        await runtime.payments.check(booking_id)  # доставит билет
        await message.answer(texts.ALREADY_PAID)
        return
    if booking.get('status') == 'Отменена':
        await message.answer(texts.PAY_EXPIRED)
        return

    runtime.remember_chat(booking_id, message.chat.id)
    runtime.payments.start_hold(booking)

    settings = runtime.settings
    await message.answer(
        _menu_text(booking, trip),
        reply_markup=keyboards.payment_methods(
            booking_id,
            payme=settings.payme_ready,
            click=settings.click_ready,
            card=settings.card_ready,
        ),
    )


async def _edit_menu(message: Message, booking_id: str) -> None:
    try:
        booking, trip = await _load(booking_id)
    except SupabaseError:
        await message.answer(texts.NO_BOOKING)
        return
    if not booking:
        await message.answer(texts.NO_BOOKING)
        return
    settings = runtime.settings
    text = _menu_text(booking, trip)
    kb = keyboards.payment_methods(
        booking_id,
        payme=settings.payme_ready,
        click=settings.click_ready,
        card=settings.card_ready,
    )
    try:
        await message.edit_text(text, reply_markup=kb)
    except Exception:  # noqa: BLE001
        await message.answer(text, reply_markup=kb)


# ---------- Способы оплаты ----------


@router.callback_query(F.data.startswith('menu:'))
async def on_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await _edit_menu(callback.message, callback.data.split(':', 1)[1])


@router.callback_query(F.data.startswith('pay:'))
async def on_pay(callback: CallbackQuery, state: FSMContext) -> None:
    _, booking_id, method = callback.data.split(':', 2)
    try:
        booking, trip = await _load(booking_id)
    except SupabaseError:
        booking = None
        trip = None
    if not booking:
        await callback.answer(texts.NO_BOOKING, show_alert=True)
        return
    if booking.get('status') == 'Оплачена':
        await callback.answer('Уже оплачено ✓', show_alert=True)
        return
    if booking.get('status') == 'Отменена':
        await callback.answer(texts.PAY_EXPIRED, show_alert=True)
        return

    settings = runtime.settings
    amount = int(booking.get('price') or 0)
    return_url = f"{settings.site_url}/hold?b={booking_id}" if settings.site_url else ''

    if method == 'payme':
        if not settings.payme_ready:
            await callback.answer('Payme пока недоступен, выберите другой способ.', show_alert=True)
            return
        link = payme_api.build_checkout_url(
            merchant_id=settings.payme_merchant_id,
            booking_id=booking_id,
            amount_uzs=amount,
            return_url=return_url,
            test=settings.payme_test,
        )
        await callback.answer('Откройте Payme по ссылке или QR-коду')
        await callback.message.answer(texts.PAY_PAYME_TEXT.format(link=link),
                                      reply_markup=keyboards.back_to_booking(booking_id))
        try:
            await callback.message.answer_photo(
                notify.qr_png(link), caption='QR для оплаты Payme'
            )
        except Exception:  # noqa: BLE001
            log.exception('QR для Payme не отправился')
        return

    if method == 'click':
        if not settings.click_ready:
            await callback.answer('Click пока недоступен, выберите другой способ.', show_alert=True)
            return
        link = click_api.build_link(
            service_id=settings.click_service_id,
            merchant_id=settings.click_merchant_id,
            booking_id=booking_id,
            amount_uzs=amount,
            return_url=return_url,
        )
        await callback.answer('Откройте ссылку на оплату')
        await callback.message.answer(texts.PAY_CLICK_TEXT.format(link=link),
                                      reply_markup=keyboards.back_to_booking(booking_id))
        return

    if method == 'card':
        if not settings.card_ready:
            await callback.answer('Перевод пока недоступен.', show_alert=True)
            return
        await runtime.payments.request_card_payment(booking, callback.message.chat.id)
        await state.set_state(Card.receipt)
        await state.update_data(booking_id=booking_id)
        await callback.answer('Ждём чек')
        await callback.message.answer(
            texts.PAY_CARD_TEXT.format(
                amount=money(amount),
                number=settings.card_number,
                holder=settings.card_holder,
            ),
            reply_markup=keyboards.back_to_booking(booking_id),
        )
        return

    await callback.answer('Неизвестный способ оплаты', show_alert=True)


# ---------- Чек по переводу ----------


@router.message(Card.receipt, F.photo)
async def on_receipt(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    booking_id = str(data.get('booking_id') or '')
    await state.clear()
    if not booking_id:
        await message.answer(texts.NO_BOOKING)
        return

    file_id = message.photo[-1].file_id
    await message.answer(texts.PAY_CARD_WAITING, reply_markup=keyboards.main_menu())

    try:
        booking = await runtime.db.booking(booking_id)
    except SupabaseError:
        booking = None
    caption = (
        f'🧾 Чек {booking_id}\n'
        f"{(booking or {}).get('trip_title', '')} · место №{(booking or {}).get('seat', '—')}\n"
        f"Сумма: {money(int((booking or {}).get('price') or 0))}\n"
        'Подтвердите или отклоните оплату:'
    )
    kb = keyboards.receipt_actions(booking_id)
    settings = runtime.settings
    for admin_id in (settings.admin_ids if settings else ()):
        try:
            await message.bot.send_photo(admin_id, file_id, caption=caption, reply_markup=kb)
        except Exception:  # noqa: BLE001
            log.exception('чек не переслался админу %s', admin_id)
            await notify.notify_admins(caption, reply_markup=kb)
            break


@router.message(Card.receipt, F.text)
async def on_receipt_text(message: Message) -> None:
    await message.answer('Пришлите, пожалуйста, скриншот чека фотографией.')


# ---------- Проверка оплаты ----------


@router.callback_query(F.data.startswith('check:'))
async def on_check(callback: CallbackQuery) -> None:
    booking_id = callback.data.split(':', 1)[1]
    try:
        status, booking = await runtime.payments.check(booking_id)
    except SupabaseError:
        await callback.answer('База недоступна, попробуйте позже.', show_alert=True)
        return

    if status == 'paid':
        await callback.answer('Оплачено ✓ Билет отправлен', show_alert=True)
        return
    if status == 'unknown':
        await callback.answer(texts.NO_BOOKING, show_alert=True)
        return
    if status == 'expired':
        await callback.answer(texts.PAY_EXPIRED, show_alert=True)
        await callback.message.answer(texts.PAY_EXPIRED, reply_markup=keyboards.main_menu())
        return
    if status == 'waiting_card':
        await callback.answer('Чек у администратора', show_alert=True)
        await callback.message.answer(
            texts.PAY_CARD_WAITING, reply_markup=keyboards.back_to_booking(booking_id)
        )
        return

    await callback.answer('Ещё не оплачено')
    await _edit_menu(callback.message, booking_id)


# ---------- Отмена брони ----------


@router.callback_query(F.data.startswith('cancel:'))
async def on_cancel_booking(callback: CallbackQuery) -> None:
    booking_id = callback.data.split(':', 1)[1]
    try:
        ok = await runtime.payments.cancel_by_user(booking_id)
    except SupabaseError:
        await callback.answer('База недоступна, попробуйте позже.', show_alert=True)
        return
    await callback.answer(texts.BOOKING_CANCELLED if ok else texts.ALREADY_PAID,
                          show_alert=True)
    await callback.message.answer(
        texts.BOOKING_CANCELLED if ok else 'Эта бронь уже закрыта.',
        reply_markup=keyboards.main_menu(),
    )
