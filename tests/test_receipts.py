"""Чек по переводу: группа админов, авто-подтверждение, отклонение, /internal/*.

Схема проверки оплаты чеком:
  1. покупатель прислал скриншот → бот кладёт фото в группу админов и
     сохраняет ссылку на пост (payments.error_note);
  2. через receipt_seconds (здесь 1 с вместо 2 минут) и никто не отклонил —
     оплата подтверждается сама, уходит билет, пост в группе обновляется;
  3. отклонение (из админки сайта или кнопкой в группе) — бронь снимается,
     место снова в продаже, покупатель получает короткий отказ;
  4. админка сайта дёргает /internal/approve|reject с X-Internal-Secret.

Запуск:  python tests/test_receipts.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from bot import runtime  # noqa: E402
from bot.payments.service import PaymentService  # noqa: E402
from bot.receipt_check import CheckResult  # noqa: E402
from bot.server import create_app  # noqa: E402
from tests.test_flows import USER_ID, FakeDb, FakeSession, settings  # noqa: E402

GROUP_ID = -100777555  # тестовая группа админов
INTERNAL_SECRET = 'int-secret-test'


def group_settings():
    return replace(settings(), admin_group_id=GROUP_ID, receipt_seconds=1,
                   internal_secret=INTERNAL_SECRET)


async def make_booking(db: FakeDb, seat: int) -> dict:
    trip = await db.trip('t1')
    return await db.create_booking({
        'id': f'BLD-9000000{seat}', 'trip_id': 't1', 'trip_title': trip['title'],
        'direction': trip['direction'], 'departure': 'Ташкент', 'destination': 'Амирsoy',
        'date': trip['date'], 'time': trip['time'], 'seat': seat, 'price': 350990,
        'lunch': True, 'first_name': 'Азиз', 'last_name': 'Каримов',
        'phone': '+998901234567', 'email': 'aziz@example.uz', 'status': 'Новая',
    })


def methods(bot: Bot, name: str) -> list:
    return [m for m in bot.session.sent if type(m).__name__ == name]


def to_chat(bot: Bot, name: str, chat_id: int) -> list:
    return [m for m in methods(bot, name) if getattr(m, 'chat_id', None) == chat_id]


async def test_group_receipt_and_auto_approve() -> None:
    """Скриншот → пост в группе со ссылкой; через 2 минуты (1 с в тесте) — оплата."""
    cfg = group_settings()
    db = FakeDb()
    service = PaymentService(db, cfg)
    bot = Bot(token='42:TEST', session=FakeSession(),
              default=DefaultBotProperties(parse_mode=None))
    runtime.bind(bot, cfg, db, service)
    try:
        booking = await make_booking(db, seat=5)
        await service.request_card_payment(booking, USER_ID)

        link = await service.register_receipt(booking['id'], 'AgACAgIAAxk', USER_ID)

        # 1. фото чека лежит в группе админов
        photos = to_chat(bot, 'SendPhoto', GROUP_ID)
        assert photos, 'чек не ушёл в группу'
        assert 'Чек' in (photos[-1].caption or ''), photos[-1].caption
        assert link == 'https://t.me/c/777555/1', link

        # 2. в платеже — признак чека и ссылка на пост
        payment = await db.payment(booking['id'])
        assert payment['error_code'] == 'receipt', payment
        assert payment['error_note'] == link, payment
        assert payment['external_id'] == f'tg:{USER_ID}', payment

        # 3. авто-подтверждение по таймеру
        await asyncio.sleep(1.3)
        booking_row = await db.booking(booking['id'])
        assert booking_row['status'] == 'Оплачена', booking_row
        payment = await db.payment(booking['id'])
        assert payment['status'] == 'paid', payment
        assert booking_row['qr_token'].startswith(booking['id']), 'нет qr_token'

        # 4. билет ушёл покупателю, пост в группе обновлён
        user_msgs = to_chat(bot, 'SendMessage', USER_ID)
        assert user_msgs, 'билет покупателю не отправлен'
        edits = methods(bot, 'EditMessageCaption')
        assert edits and 'автоматически' in (edits[-1].caption or ''), \
            [getattr(m, 'caption', None) for m in edits]
        print('ok: чек в группе → ссылка → авто-подтверждение через 2 минуты')
    finally:
        await runtime.shutdown()


async def test_reject_frees_seat() -> None:
    """Отклонение: бронь снята (место в продаже), короткий отказ покупателю, таймер снят."""
    cfg = group_settings()
    db = FakeDb()
    service = PaymentService(db, cfg)
    bot = Bot(token='42:TEST', session=FakeSession(),
              default=DefaultBotProperties(parse_mode=None))
    runtime.bind(bot, cfg, db, service)
    try:
        booking = await make_booking(db, seat=7)
        await service.request_card_payment(booking, USER_ID)
        await service.register_receipt(booking['id'], 'AgACAgIAAxk', USER_ID)

        await service.reject_payment(booking['id'], 'Чек чужой')

        booking_row = await db.booking(booking['id'])
        assert booking_row['status'] == 'Отменена', booking_row  # место снова свободно
        payment = await db.payment(booking['id'])
        assert payment['status'] == 'cancelled' and payment['error_code'] == 'rejected', payment

        user_text = ' | '.join(getattr(m, 'text', None) or '' for m in to_chat(bot, 'SendMessage', USER_ID))
        assert 'Оплата не подтверждена' in user_text, user_text

        # пост в группе помечен отклонённым
        edits = methods(bot, 'EditMessageCaption')
        assert edits and 'Чек отклонён' in (edits[-1].caption or ''), \
            [getattr(m, 'caption', None) for m in edits]

        # таймер погашен: авто-подтверждения после отклонения не будет
        await asyncio.sleep(1.3)
        booking_row = await db.booking(booking['id'])
        assert booking_row['status'] == 'Отменена', booking_row
        print('ok: отклонение снимает бронь, место снова в продаже, таймер отменён')
    finally:
        await runtime.shutdown()


async def test_suspect_receipt_waits_for_admin() -> None:
    """Автопроверка нашла проблему: через 2 минуты чек НЕ принимается сам, ждёт админа."""
    cfg = group_settings()
    db = FakeDb()
    service = PaymentService(db, cfg)
    bot = Bot(token='42:TEST', session=FakeSession(),
              default=DefaultBotProperties(parse_mode=None))
    runtime.bind(bot, cfg, db, service)
    try:
        booking = await make_booking(db, seat=9)
        await service.request_card_payment(booking, USER_ID)

        # Авто-сказала: сумма не совпала (подменяем анализатор, OCR в тестах не гоняем).
        service.receipt_analyzer = lambda photo, **kw: CheckResult(
            verdict='suspect', reasons=['сумма в чеке 100 000 не совпадает с 350 990'],
        )
        await service.register_receipt(booking['id'], 'f-suspect', USER_ID)

        # 1. в посте админам — предупреждение автопроверки
        photos = to_chat(bot, 'SendPhoto', GROUP_ID)
        assert photos, 'чек не ушёл в группу'
        caption = photos[-1].caption or ''
        assert '⚠️ Автопроверка' in caption and 'вручную' in caption, caption

        # 2. прошло 2 минуты — авто-принятия нет, бронь ждёт админа
        await asyncio.sleep(1.3)
        booking_row = await db.booking(booking['id'])
        assert booking_row['status'] == 'Новая', booking_row
        payment = await db.payment(booking['id'])
        assert payment['status'] == 'pending', payment

        # 3. админ подтвердил вручную — оплата проходит
        assert await service.approve_card(booking['id']) is True
        assert db.bookings[booking['id']]['status'] == 'Оплачена'
        print('ok: сомнительный чек не принимается автоматически — ждёт админа')
    finally:
        await runtime.shutdown()


def test_internal_endpoints() -> None:
    """Админка сайта: /internal/approve и /internal/reject под секретом."""
    cfg = group_settings()
    db = FakeDb()
    service = PaymentService(db, cfg)
    bot = Bot(token='42:TEST', session=FakeSession(),
              default=DefaultBotProperties(parse_mode=None))
    runtime.bind(bot, cfg, db, service)
    app = create_app(service, cfg, db, dispatcher=None, bot=None)
    client = TestClient(app)
    try:
        loop = asyncio.new_event_loop()
        approve_booking = loop.run_until_complete(make_booking(db, seat=11))
        reject_booking = loop.run_until_complete(make_booking(db, seat=13))
        approve_id, reject_id = approve_booking['id'], reject_booking['id']
        loop.run_until_complete(service.request_card_payment(approve_booking, USER_ID))
        loop.run_until_complete(service.register_receipt(approve_id, 'f1', USER_ID))
        loop.run_until_complete(service.request_card_payment(reject_booking, USER_ID))
        loop.run_until_complete(service.register_receipt(reject_id, 'f2', USER_ID))

        # без секрета / с чужим секретом — отказ
        assert client.post('/internal/approve', json={'booking_id': approve_id}).status_code == 403
        bad = client.post('/internal/reject', json={'booking_id': reject_id},
                          headers={'x-internal-secret': 'nope'})
        assert bad.status_code == 403, bad.status_code

        # без booking_id — 400
        missing = client.post('/internal/approve', json={},
                              headers={'x-internal-secret': INTERNAL_SECRET})
        assert missing.status_code == 400, missing.status_code

        # подтверждение → оплачено
        ok = client.post('/internal/approve', json={'booking_id': approve_id},
                         headers={'x-internal-secret': INTERNAL_SECRET})
        assert ok.status_code == 200 and ok.json().get('ok') is True, ok.text
        assert db.bookings[approve_id]['status'] == 'Оплачена', db.bookings[approve_id]

        # отклонение → бронь снята
        ok = client.post('/internal/reject',
                         json={'booking_id': reject_id, 'reason': 'Нет платежа'},
                         headers={'x-internal-secret': INTERNAL_SECRET})
        assert ok.status_code == 200 and ok.json().get('ok') is True, ok.text
        assert db.bookings[reject_id]['status'] == 'Отменена', db.bookings[reject_id]

        # секрет не задан вовсе — 503
        no_secret = replace(cfg, internal_secret='')
        app2 = create_app(service, no_secret, db, dispatcher=None, bot=None)
        assert TestClient(app2).post('/internal/approve', json={'booking_id': approve_id},
                                     ).status_code == 503
        print('ok: /internal/* принимает только правильный секрет')
    finally:
        service.stop()  # гасим таймеры, пока loop ещё жив
        loop.run_until_complete(asyncio.sleep(0.05))
        loop.close()
        asyncio.run(runtime.shutdown())


def main() -> None:
    asyncio.run(test_group_receipt_and_auto_approve())
    asyncio.run(test_reject_frees_seat())
    asyncio.run(test_suspect_receipt_waits_for_admin())
    test_internal_endpoints()
    print('\n4 сценария чека — прошли')


if __name__ == '__main__':
    main()
