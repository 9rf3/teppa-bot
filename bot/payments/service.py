"""Сервис оплаты: удержание места на 12 минут, отметка об оплате, доставка билета.

Единая точка, куда сходятся все способы оплаты — Payme (вебхук Merchant API),
Click (Prepare/Complete) и перевод на карту (подтверждает администратор).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .. import keyboards, notify, receipt_check, runtime, texts
from ..config import Settings
from ..db import Supabase, SupabaseError
from ..receipt_check import CheckResult
from ..utils import (
    create_qr_token,
    hold_deadline,
    hold_left,
    mmss,
    money,
    parse_tg_post,
    seconds_since,
    tg_post_link,
)

log = logging.getLogger('teppa.payments')


class PaymentService:
    def __init__(self, db: Supabase, settings: Settings):
        self.db = db
        self.settings = settings
        # booking_id -> задача авто-подтверждения чека (через receipt_seconds).
        self._receipts: dict[str, asyncio.Task] = {}
        # booking_id -> вердикт машинной проверки чека (bot/receipt_check.py).
        self._checks: dict[str, CheckResult] = {}
        # booking_id -> file_id фото чека: понадобится, если авто-принятие
        # не сработает — перешлём админам в ЛС вместе с текстом.
        self._receipt_files: dict[str, str] = {}
        # Хэши уже присланных фото — чтобы один чек не прикладывали дважды.
        self._seen_hashes: set[str] = set()
        # Подменяется в тестах: сигнатура как у receipt_check.analyze.
        self.receipt_analyzer = receipt_check.analyze

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

            chat_id = await self._buyer_chat(booking_id)
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

    async def request_card_payment(self, booking: dict[str, Any], chat_id: int,
                                   provider: str = 'card') -> None:
        """Покупатель начал платить (карта/Payme/Click): платёж в очередь подтверждения."""
        await self.db.save_payment(
            booking_id=str(booking['id']),
            provider=provider,
            amount=int(booking.get('price') or 0),
            status='pending',
            external_id=f'tg:{chat_id}',
            error_note='Ожидает подтверждения чека',
        )
        runtime.remember_chat(str(booking['id']), chat_id)

    async def _buyer_chat(self, booking_id: str) -> int | None:
        """Чат покупателя: память процесса, потом payments.external_id ('tg:<id>')."""
        chat_id = runtime.chat_for(booking_id)
        if chat_id:
            return chat_id
        try:
            payment = await self.db.payment(booking_id)
        except SupabaseError:
            payment = None
        external = str((payment or {}).get('external_id') or '')
        if external.startswith('tg:'):
            try:
                chat_id = int(external.split(':', 1)[1])
            except ValueError:
                chat_id = None
        if chat_id:
            runtime.remember_chat(booking_id, chat_id)
        return chat_id

    async def approve_card(self, booking_id: str, *, auto: bool = False) -> bool:
        """Оплата подтверждена (вручную или автоматически через 2 минуты) — билет уходит."""
        payment = await self.db.payment(booking_id)
        external = str((payment or {}).get('external_id') or '')
        provider = str((payment or {}).get('provider') or 'card')
        ok = await self.mark_paid(
            booking_id,
            provider=provider,
            external_id=external or None,
            note='Оплачено автоматически по чеку' if auto else 'Подтверждено администратором',
        )
        if ok:
            self._checks.pop(booking_id, None)
            self._receipt_files.pop(booking_id, None)
            await notify.notify_user(booking_id, texts.PAID_BADGE)
            await self._edit_receipt_post(
                payment,
                texts.RECEIPT_POST_AUTO_OK if auto else texts.RECEIPT_POST_OK,
            )
        return ok

    async def reject_payment(self, booking_id: str, reason: str = '') -> None:
        """Чек отклонён: снимаем бронь (место снова в продаже) и оповещаем покупателя.

        Работает и до авто-подтверждения, и после — билет перестаёт действовать,
        потому что проверка QR смотрит на статус брони.
        """
        note = reason or 'Чек не подтверждён'
        booking = await self.db.booking(booking_id)
        payment = await self.db.payment(booking_id)

        if booking and booking.get('status') != 'Отменена':
            try:
                await self.db.set_booking_status(booking_id, 'Отменена')
            except SupabaseError:
                log.exception('не удалось снять бронь %s', booking_id)

        if payment and payment.get('status') != 'cancelled':
            await self.db.save_payment(
                booking_id=booking_id,
                provider=str(payment.get('provider') or 'card'),
                amount=int(payment.get('amount') or 0),
                status='cancelled',
                error_code='rejected',
                error_note=note,
            )

        self.cancel_receipt_check(booking_id)
        runtime.cancel_hold_task(booking_id)
        self._checks.pop(booking_id, None)
        self._receipt_files.pop(booking_id, None)

        await notify.notify_user(booking_id, texts.PAY_REJECTED, keyboards.main_menu())

        email = str((booking or {}).get('email') or '').strip()
        if email and self.settings.smtp_ready:
            await asyncio.to_thread(
                notify.send_email,
                email,
                texts.EMAIL_REJECT_SUBJECT,
                texts.EMAIL_REJECT_BODY.format(booking=booking_id),
            )

        await self._edit_receipt_post(payment, texts.RECEIPT_POST_REJECTED.format(reason=note))
        await notify.notify_admins(
            f'❌ Бронь {booking_id} отклонена — {note}. Место снова свободно.'
        )

    # ---------- Чек: пересылка в группу и авто-подтверждение ----------

    async def register_receipt(self, booking_id: str, file_id: str, chat_id: int,
                               provider: str = 'card') -> str:
        """Скриншот получен: машинная проверка → фото в группу админов, таймер.

        Возвращает ссылку на пост в группе ('' — группа не настроена, чек ушёл в ЛС).
        """
        booking = await self.db.booking(booking_id)
        booking = booking or {}
        passenger = f"{booking.get('first_name') or ''} {booking.get('last_name') or ''}".strip()

        # Машинная проверка (bot/receipt_check.py): фото, дубликат, сумма, дата.
        # Пока фото скачивается и разбирается — покупатель ещё ничего не знает.
        photo = await self._download_receipt(file_id)
        if len(self._seen_hashes) > 2000:  # память: старые хэши просто забываем
            self._seen_hashes.clear()
        result = await asyncio.to_thread(
            self.receipt_analyzer,
            photo,
            expected_amount=int(booking.get('price') or 0),
            seen=self._seen_hashes,
        )
        self._checks[booking_id] = result
        self._receipt_files[booking_id] = file_id
        log.info('чек %s: автопроверка — %s (%s)', booking_id, result.verdict,
                 '; '.join(result.reasons) or '; '.join(result.checked) or 'без деталей')

        caption = texts.RECEIPT_GROUP_CAPTION.format(
            booking=booking_id,
            title=booking.get('trip_title') or booking.get('direction') or 'Поездка',
            seat=booking.get('seat') or '—',
            amount=money(int(booking.get('price') or 0)),
            passenger=passenger or '—',
            phone=booking.get('phone') or '—',
        ) + '\n\n' + receipt_check.describe(result)
        link = await self._post_receipt(file_id, caption, keyboards.receipt_actions(booking_id))

        runtime.remember_chat(booking_id, chat_id)
        await self.db.save_payment(
            booking_id=booking_id,
            provider=provider,
            amount=int(booking.get('price') or 0),
            status='pending',
            external_id=f'tg:{chat_id}',
            error_code='receipt',
            error_note=link or 'Чек у администратора',
        )
        self.schedule_receipt_check(booking_id)
        return link

    async def _download_receipt(self, file_id: str) -> bytes | None:
        """Скачивает фото чека с серверов Telegram (None — не скачалось)."""
        bot = runtime.bot
        if not bot:
            return None
        try:
            file = await bot.get_file(file_id)
            data = await bot.download_file(file.file_path)
            if data is None:
                return None
            raw = data.getvalue() if hasattr(data, 'getvalue') else bytes(data)
            return raw or None
        except Exception:  # noqa: BLE001
            log.exception('фото чека не скачалось: %s', file_id)
            return None

    async def _post_receipt(self, file_id: str, caption: str, markup) -> str:
        """Фото чека → группа админов; если не вышло — в ЛС админам. Ссылка на пост."""
        bot = runtime.bot
        group = self.settings.admin_group_id
        if bot and group:
            try:
                message = await bot.send_photo(group, file_id, caption=caption, reply_markup=markup)
                return tg_post_link(group, message.message_id)
            except Exception:  # noqa: BLE001
                log.exception('чек не ушёл в группу %s — отправляем админам в ЛС', group)
        if bot:
            for admin_id in self.settings.admin_ids:
                try:
                    await bot.send_photo(admin_id, file_id, caption=caption, reply_markup=markup)
                except Exception:  # noqa: BLE001
                    log.exception('чек не переслался админу %s', admin_id)
        return ''

    def schedule_receipt_check(self, booking_id: str, delay: float | None = None) -> None:
        """Через delay секунд (по умолчанию receipt_seconds) чек принимается сам."""
        if delay is None:
            delay = float(self.settings.receipt_seconds)
        old = self._receipts.pop(booking_id, None)
        if old and not old.done():
            old.cancel()
        self._receipts[booking_id] = asyncio.create_task(self._receipt_watch(booking_id, delay))

    def cancel_receipt_check(self, booking_id: str) -> None:
        task = self._receipts.pop(booking_id, None)
        if task and not task.done():
            task.cancel()

    def stop(self) -> None:
        """Гасит таймеры чеков при выключении процесса."""
        for task in self._receipts.values():
            if not task.done():
                task.cancel()
        self._receipts.clear()
        self._checks.clear()
        self._receipt_files.clear()

    async def _receipt_watch(self, booking_id: str, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self.auto_accept_receipt(booking_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception('авто-подтверждение чека не отработало: %s', booking_id)

    async def auto_accept_receipt(self, booking_id: str) -> bool:
        """Прошло 2 минуты, никто не отклонил и автопроверка не нашла проблем —
        принимаем оплату, шлём билет. Сомнительный чек ждёт админа."""
        payment = await self.db.payment(booking_id)
        if not payment or payment.get('status') != 'pending' \
                or payment.get('error_code') != 'receipt':
            return False
        booking = await self.db.booking(booking_id)
        if not booking or booking.get('status') != 'Новая':
            return False
        verdict = self._checks.get(booking_id)
        if verdict is not None and not verdict.auto_accept:
            log.warning('чек %s не принят автоматически: %s', booking_id,
                        '; '.join(verdict.reasons))
            await self._notify_manual_review(booking_id, verdict)
            return False
        return await self.approve_card(booking_id, auto=True)

    async def _notify_manual_review(self, booking_id: str, verdict: CheckResult) -> None:
        """Автопроверка не пройдена: предупреждение в группу, админам в ЛС —
        фото чека с кнопками, покупателю — «ждите администратора»."""
        reasons = '; '.join(verdict.reasons) or 'автопроверка не пройдена'
        bot = runtime.bot

        # 1. Группа админов — сюда же сбежались чеки и кнопки ✅/❌.
        group = self.settings.admin_group_id
        if bot and group:
            try:
                await bot.send_message(
                    group, texts.GROUP_MANUAL_WAIT.format(booking=booking_id, reasons=reasons)
                )
            except Exception:  # noqa: BLE001
                log.exception('предупреждение не ушло в группу %s', group)

        # 2. Админам в личку бота — чтобы можно было проверить, не открывая группу.
        caption = texts.ADMIN_RECEIPT_NOT_APPROVED.format(booking=booking_id, reasons=reasons)
        file_id = self._receipt_files.get(booking_id)
        markup = keyboards.receipt_actions(booking_id)
        if bot and file_id:
            for admin_id in self.settings.admin_ids:
                try:
                    await bot.send_photo(admin_id, file_id, caption=caption,
                                         reply_markup=markup)
                except Exception:  # noqa: BLE001
                    log.exception('чек не переслался админу %s', admin_id)
        else:
            await notify.notify_admins(caption, reply_markup=markup)

        # 3. Покупателю — чек не подтверждён автоматически, ждём человека.
        await notify.notify_user(booking_id, texts.PAY_MANUAL_WAIT)

    async def resume_receipt_checks(self) -> None:
        """После рестарта: дожидаемся остатка таймера, просроченные — сразу принимаем."""
        try:
            payments = await self.db.pending_receipts()
        except SupabaseError:
            log.exception('не удалось возобновить проверку чеков')
            return
        for payment in payments:
            booking_id = str(payment.get('booking_id') or '')
            if not booking_id:
                continue
            waited = seconds_since(str(payment.get('updated_at') or payment.get('created_at') or ''))
            self.schedule_receipt_check(booking_id, max(0.0, self.settings.receipt_seconds - waited))

    async def _edit_receipt_post(self, payment: dict[str, Any] | None, title: str) -> None:
        """Итог проверки на посте с чеком: правим подпись, гасим кнопки."""
        link = str((payment or {}).get('error_note') or '')
        ref = parse_tg_post(link)
        bot = runtime.bot
        if not ref or not bot:
            return
        try:
            await bot.edit_message_caption(
                chat_id=ref[0], message_id=ref[1], caption=title, reply_markup=None,
            )
        except Exception:  # noqa: BLE001
            log.exception('не удалось обновить пост чека %s', link)

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
        if payment and payment.get('status') == 'pending' \
                and payment.get('error_code') == 'receipt':
            return 'waiting_card', booking

        deadline = hold_deadline(str(booking.get('created_at') or ''), self.settings.hold_minutes)
        if hold_left(deadline).total_seconds() <= 0:
            await self.expire(booking_id)
            return 'expired', booking

        return 'unpaid', booking

    async def _deliver_ticket(self, booking: dict[str, Any]) -> None:
        chat_id = await self._buyer_chat(str(booking['id']))
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
