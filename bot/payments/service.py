"""Сервис оплаты: удержание места на 12 минут, отметка об оплате, доставка билета.

Единая точка, куда сходятся все способы оплаты — Payme (вебхук Merchant API),
Click (Prepare/Complete) и перевод на карту (подтверждает администратор).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .. import keyboards, notify, runtime, texts
from ..config import Settings
from ..db import Supabase, SupabaseError
from ..utils import create_qr_token, hold_deadline, hold_left, mmss, money

log = logging.getLogger('teppa.payments')


class PaymentService:
    def __init__(self, db: Supabase, settings: Settings):
        self.db = db
        self.settings = settings

    # ---------- Удержание места ----------

    def start_hold(self, booking: dict[str, Any]) -> None:
        """Ставит задачу: не заплатили за N минут — снимаем бронь, место свободно."""
        booking_id = str(booking['id'])
        task = asyncio.create_task(
            self._hold_watch(booking_id, str(booking.get('created_at') or ''))
        )
        runtime.set_hold_task(booking_id, task)

    async def _hold_watch(self, booking_id: str, created_at: str) -> None:
        deadline = hold_deadline(created_at, self.settings.hold_minutes)
        left = hold_left(deadline)
        if left.total_seconds() > 0:
            await asyncio.sleep(left.total_seconds())

        booking = await self.db.booking(booking_id)
        if booking and booking.get('status') == 'Новая':
            await self.expire(booking_id)

    async def expire(self, booking_id: str) -> None:
        """Время вышло: реально отменяем бронь — место снова свободно."""
        booking = await self.db.booking(booking_id)
        if not booking or booking.get('status') != 'Новая':
            return

        try:
            await self.db.set_booking_status(booking_id, 'Отменена')
        except SupabaseError:
            log.exception('не удалось отменить бронь %s', booking_id)
            return

        payment = await self.db.payment(booking_id)
        if payment and payment.get('status') == 'pending':
            await self.db.save_payment(
                booking_id=booking_id,
                provider=str(payment.get('provider') or 'card'),
                amount=int(payment.get('amount') or 0),
                status='cancelled',
                error_code='hold_expired',
                error_note='Время удержания истекло',
            )

        runtime.cancel_hold_task(booking_id)
        await notify.notify_user(booking_id, texts.PAY_EXPIRED)
        await notify.notify_admins(
            texts.ADMIN_EXPIRED.format(
                booking=booking_id, seat=booking.get('seat') or '—'
            )
        )

    async def cancel_by_user(self, booking_id: str) -> bool:
        booking = await self.db.booking(booking_id)
        if not booking or booking.get('status') != 'Новая':
            return False
        await self.db.set_booking_status(booking_id, 'Отменена')
        payment = await self.db.payment(booking_id)
        if payment and payment.get('status') == 'pending':
            await self.db.save_payment(
                booking_id=booking_id,
                provider=str(payment.get('provider') or 'card'),
                amount=int(payment.get('amount') or 0),
                status='cancelled',
                error_code='user_cancelled',
                error_note='Отменено покупателем',
            )
        runtime.cancel_hold_task(booking_id)
        return True

    # ---------- Оплата ----------

    async def mark_paid(
        self,
        booking_id: str,
        *,
        provider: str,
        external_id: str | None = None,
        note: str = '',
    ) -> bool:
        """Бронь оплачена: платёж → paid, бронь → «Оплачена», билет покупателю."""
        booking = await self.db.booking(booking_id)
        if not booking:
            log.warning('оплата для неизвестной брони %s', booking_id)
            return False

        already = booking.get('status') == 'Оплачена'

        if not already:
            await self.db.save_payment(
                booking_id=booking_id,
                provider=provider,
                amount=int(booking.get('price') or 0),
                status='paid',
                external_id=external_id,
                error_code=None,
                error_note=note or None,
            )
            await self.db.set_booking_status(booking_id, 'Оплачена')

            # QR-токен — в формате сайта, пишем один раз.
            if not booking.get('qr_token'):
                token = create_qr_token(booking_id)
                try:
                    await self.db.set_qr_token(booking_id, token)
                    booking['qr_token'] = token
                except SupabaseError:
                    log.exception('не удалось записать qr_token %s', booking_id)

        runtime.cancel_hold_task(booking_id)

        if not already:
            trip = None
            try:
                trip = await self.db.trip(str(booking.get('trip_id') or ''))
            except SupabaseError:
                log.exception('не удалось загрузить рейс %s', booking.get('trip_id'))

            chat_id = runtime.chat_for(booking_id)
            if chat_id and runtime.bot:
                await notify.send_ticket(runtime.bot, chat_id, booking, trip)

            await notify.notify_admins(
                texts.ADMIN_PAID.format(
                    booking=booking_id,
                    title=booking.get('trip_title') or booking.get('direction') or '',
                    seat=booking.get('seat') or '—',
                    amount=money(int(booking.get('price') or 0)),
                    provider=provider,
                )
            )
        return True

    async def mark_cancelled(self, booking_id: str, reason: str) -> None:
        payment = await self.db.payment(booking_id)
        if payment and payment.get('status') != 'cancelled':
            await self.db.save_payment(
                booking_id=booking_id,
                provider=str(payment.get('provider') or 'payme'),
                amount=int(payment.get('amount') or 0),
                status='cancelled',
                error_note=reason or None,
            )
        await notify.notify_user(booking_id, f'Оплата отменена: {reason}')

    # ---------- Перевод на карту ----------

    async def request_card_payment(self, booking: dict[str, Any], chat_id: int) -> None:
        """Покупатель выбрал перевод: кладём платёж в очередь подтверждения."""
        await self.db.save_payment(
            booking_id=str(booking['id']),
            provider='card',
            amount=int(booking.get('price') or 0),
            status='pending',
            external_id=f'tg:{chat_id}',
            error_note='Ожидает подтверждения чека',
        )
        runtime.remember_chat(str(booking['id']), chat_id)

    async def approve_card(self, booking_id: str) -> bool:
        payment = await self.db.payment(booking_id)
        external = str((payment or {}).get('external_id') or '')
        ok = await self.mark_paid(
            booking_id,
            provider='card',
            external_id=external or None,
            note='Подтверждено администратором',
        )
        if ok:
            await notify.notify_user(booking_id, texts.PAID_BADGE)
        return ok

    async def reject_card(self, booking_id: str) -> None:
        payment = await self.db.payment(booking_id)
        if payment and payment.get('status') == 'pending':
            await self.db.save_payment(
                booking_id=booking_id,
                provider='card',
                amount=int(payment.get('amount') or 0),
                status='cancelled',
                error_code='rejected',
                error_note='Чек отклонён администратором',
            )
        await notify.notify_user(
            booking_id,
            'Чек не подтверждён. Пришлите корректный скриншот или выберите другой способ оплаты.',
            keyboards.back_to_booking(booking_id),
        )

    # ---------- Проверка ----------

    async def check(self, booking_id: str) -> tuple[str, dict[str, Any] | None]:
        """«Проверить оплату»: (код, бронь). Коды: paid | waiting_card | unpaid | expired | unknown."""
        booking = await self.db.booking(booking_id)
        if not booking:
            return 'unknown', None

        if booking.get('status') == 'Оплачена':
            await self._deliver_ticket(booking)
            return 'paid', booking
        if booking.get('status') == 'Отменена':
            return 'expired', booking

        payment = await self.db.payment(booking_id)
        if payment and payment.get('provider') == 'card' and payment.get('status') == 'pending':
            return 'waiting_card', booking

        deadline = hold_deadline(str(booking.get('created_at') or ''), self.settings.hold_minutes)
        if hold_left(deadline).total_seconds() <= 0:
            await self.expire(booking_id)
            return 'expired', booking

        return 'unpaid', booking

    async def _deliver_ticket(self, booking: dict[str, Any]) -> None:
        chat_id = runtime.chat_for(str(booking['id']))
        if not chat_id or not runtime.bot:
            return
        trip = None
        try:
            trip = await self.db.trip(str(booking.get('trip_id') or ''))
        except SupabaseError:
            log.exception('не удалось загрузить рейс %s', booking.get('trip_id'))
        await notify.send_ticket(runtime.bot, chat_id, booking, trip)

    def hold_text(self, booking: dict[str, Any]) -> str:
        deadline = hold_deadline(str(booking.get('created_at') or ''), self.settings.hold_minutes)
        return texts.HOLD_LEFT.format(time=mmss(hold_left(deadline)))
