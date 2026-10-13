"""Сквозной тест обработчиков: реальные апдейты гоняются через Dispatcher.

Ни сети, ни Telegram, ни Supabase: сессия бота — заглушка, которая записывает
отправленное, база — память. Проверяется вся цепочка:

  /start → рейсы → карточка → место → пассажир → телефон → email → обед →
  подтверждение → меню оплаты → чек по переводу → подтверждение админом →
  билет с QR → проверка статуса → /admin

Такой тест ловит ошибки вроде `command.payload` (его в aiogram 3 нет — есть
`command.args`), из-за которых бот молчит.

Запуск:  python tests/test_flows.py
"""

from __future__ import annotations

import itertools
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.types import Chat, Message, Update  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402

from bot import runtime  # noqa: E402
from bot.config import Settings  # noqa: E402
from bot.db import SeatConflict  # noqa: E402
from bot.handlers import register_all  # noqa: E402
from bot.payments.service import PaymentService  # noqa: E402

USER_ID = 777000
NOW = datetime.now(timezone.utc)

STEP = itertools.count(1)


# ---------- Заглушка сессии Telegram ----------


def _response_for(method):
    returning = method.__returning__
    if returning is bool:
        return True
    if getattr(returning, '__name__', '') == 'Message':
        chat_id = getattr(method, 'chat_id', USER_ID)
        return Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=chat_id if isinstance(chat_id, int) else USER_ID, type='private'),
            text=getattr(method, 'text', None),
            caption=getattr(method, 'caption', None),
        )
    try:
        return returning()
    except Exception:  # noqa: BLE001
        return True


class FakeSession(BaseSession):
    """Записывает вызовы Bot API вместо отправки в Telegram."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list = []

    async def close(self) -> None:
        pass

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536,
                             raise_for_status=True):  # noqa: ANN001
        yield b''

    async def make_request(self, bot, method, timeout=None):  # noqa: ANN001
        self.sent.append(method)
        return _response_for(method)


# ---------- База в памяти ----------


class FakeDb:
    def __init__(self) -> None:
        future = (date.today() + timedelta(days=30)).isoformat()
        self.trips = [
            {'id': 't1', 'title': 'Amirsoy Day', 'direction': 'Ташкент → Амирsoy',
             'departure': 'Ташкент', 'destination': 'Амирsoy', 'date': future, 'time': '06:00',
             'price': 250000, 'seats': 20, 'status': 'published', 'blocked_seats': []},
            {'id': 'gone', 'title': 'Ушедший рейс', 'direction': 'Ташкент → Чымган',
             'date': '2000-01-01', 'time': '06:00', 'price': 100000, 'seats': 20,
             'status': 'published', 'blocked_seats': []},
            {'id': 'draft', 'title': 'Черновик', 'direction': 'Ташкент → Заамин',
             'date': '2099-02-02', 'time': '06:00', 'price': 100000, 'seats': 20,
             'status': 'draft', 'blocked_seats': []},
        ]
        self.bookings: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}

    async def close(self) -> None:
        pass

    async def upcoming_trips(self):
        today = date.today().isoformat()
        return [t for t in self.trips if t['status'] == 'published' and t['date'] >= today]

    async def trip(self, trip_id):
        return next((t for t in self.trips if t['id'] == trip_id), None)

    async def booking(self, booking_id):
        row = self.bookings.get(booking_id)
        return dict(row) if row else None

    async def bookings_by_trip(self, trip_id):
        return [{'seat': b['seat'], 'status': b['status']}
                for b in self.bookings.values()
                if b['trip_id'] == trip_id and b['status'] != 'Отменена']

    async def bookings_by_phone(self, phone):
        digits = ''.join(ch for ch in phone if ch.isdigit())[-9:]
        return [dict(b) for b in self.bookings.values()
                if digits and ''.join(c for c in (b.get('phone') or '') if c.isdigit()).endswith(digits)]

    async def bookings_since(self, since_iso):
        return [{'status': b['status'], 'price': b['price']}
                for b in self.bookings.values() if b['created_at'] >= since_iso]

    async def create_booking(self, payload):
        busy = [b for b in self.bookings.values()
                if b['trip_id'] == payload['trip_id'] and b['seat'] == payload['seat']
                and b['status'] != 'Отменена']
        if busy:
            raise SeatConflict('Место уже забронировано')
        row = {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'updated_at': None,
            'qr_token': None,
            'checked_in_at': None,
            'user_id': None,
            **payload,
        }
        self.bookings[row['id']] = row
        return dict(row)

    async def set_booking_status(self, booking_id, status):
        self.bookings[booking_id]['status'] = status
        self.bookings[booking_id]['updated_at'] = datetime.now(timezone.utc).isoformat()

    async def set_qr_token(self, booking_id, token):
        self.bookings[booking_id]['qr_token'] = token

    async def payment(self, booking_id):
        row = self.payments.get(booking_id)
        return dict(row) if row else None

    async def payment_by_external_id(self, external_id):
        return next((dict(p) for p in self.payments.values()
                     if p.get('external_id') == external_id), None)

    async def pending_card_payments(self):
        return [dict(p) for p in self.payments.values()
                if p['provider'] == 'card' and p['status'] == 'pending']

    async def pending_receipts(self):
        return [dict(p) for p in self.payments.values()
                if p['provider'] == 'card' and p['status'] == 'pending'
                and p.get('error_code') == 'receipt']

    async def save_payment(self, *, booking_id, provider, amount, status,
                           external_id=None, prepare_id=None, error_code=None, error_note=None):
        base = self.payments.get(booking_id, {'created_at': datetime.now(timezone.utc).isoformat()})
        base.update({'booking_id': booking_id, 'provider': provider, 'amount': amount,
                     'status': status, 'updated_at': datetime.now(timezone.utc).isoformat()})
        for key, value in (('external_id', external_id), ('prepare_id', prepare_id),
                           ('error_code', error_code), ('error_note', error_note)):
            if value is not None:
                base[key] = value
        self.payments[booking_id] = base


# ---------- Апдейты ----------


def _from():
    return {'id': USER_ID, 'is_bot': False, 'first_name': 'Азиз', 'last_name': 'Каримов',
            'username': 'tester'}


def _chat():
    return {'id': USER_ID, 'type': 'private'}


def text_update(text: str) -> Update:
    n = next(STEP)
    return Update.model_validate({
        'update_id': n,
        'message': {'message_id': n, 'date': NOW.isoformat(), 'chat': _chat(),
                    'from': _from(), 'text': text},
    })


def contact_update(phone: str) -> Update:
    n = next(STEP)
    return Update.model_validate({
        'update_id': n,
        'message': {'message_id': n, 'date': NOW.isoformat(), 'chat': _chat(),
                    'from': _from(), 'contact': {'phone_number': phone, 'user_id': USER_ID,
                                                 'first_name': 'Азиз'}},
    })


def photo_update() -> Update:
    n = next(STEP)
    return Update.model_validate({
        'update_id': n,
        'message': {'message_id': n, 'date': NOW.isoformat(), 'chat': _chat(),
                    'from': _from(),
                    'photo': [{'file_id': 'AgACAgIAAxk', 'file_unique_id': 'AQAD',
                               'width': 90, 'height': 90}]},
    })


def callback_update(data: str) -> Update:
    n = next(STEP)
    return Update.model_validate({
        'update_id': n,
        'callback_query': {
            'id': f'cb{n}', 'from': _from(), 'chat_instance': 'ci', 'data': data,
            'message': {'message_id': n, 'date': NOW.isoformat(), 'chat': _chat(),
                        'from': {'id': USER_ID, 'is_bot': True, 'first_name': 'Teppa'}},
        },
    })


def markup_text(method) -> str:
    """Подписи кнопок: и inline-, и reply-клавиатуры."""
    kb = getattr(method, 'reply_markup', None)
    if not kb:
        return ''
    labels: list[str] = []
    for row in (getattr(kb, 'inline_keyboard', None) or getattr(kb, 'keyboard', None) or []):
        for button in row:
            label = getattr(button, 'text', None)
            if label:
                labels.append(label)
    return ' '.join(labels)


def describe(method, limit: int = 0) -> str:
    name = type(method).__name__
    body = getattr(method, 'text', None) or getattr(method, 'caption', None) or ''
    body = f'{body} [{markup_text(method)}]'.strip()
    if limit:
        body = body[:limit]
    return f'{name}: {body}'


# ---------- Сценарий ----------


def settings() -> Settings:
    return Settings(
        bot_token='42:TEST', bot_username='teppa_pay_bot', admin_ids=(USER_ID,),
        supabase_url='https://example.supabase.co', supabase_service_key='service-key',
        site_url='https://teppa.uz',
        payme_merchant_id='MerchantId', payme_secret_key='Secret', payme_test=True,
        click_service_id='1', click_merchant_id='2', click_secret_key='3',
        card_number='8600 1111 2222 3333', card_holder='TEPPA',
    )


async def run() -> None:
    cfg = settings()
    db = FakeDb()
    service = PaymentService(db, cfg)
    bot = Bot(token='42:TEST', session=FakeSession(),
              default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    register_all(dp)
    runtime.bind(bot, cfg, db, service)

    log: list[str] = []

    async def send(update: Update, expect: str) -> str:
        before = len(bot.session.sent)
        await dp.feed_update(bot=bot, update=update)
        fresh = bot.session.sent[before:]
        assert fresh, f'бот промолчал на {expect!r}'
        text = ' | '.join(describe(m) for m in fresh)
        log.append(f'  {describe(fresh[-1], limit=150)}')
        assert expect in text, f'ожидали {expect!r} в ответе, получили:\n{text}'
        return text

    try:
        # 1. старт
        await send(text_update('/start'), 'TEPPA')
        # 2. список рейсов: ушедшие и черновики скрыты
        out = await send(text_update('🚍 Рейсы'), 'Ближайшие поездки')
        assert 'Amirsoy Day' in out and 'Ушедший рейс' not in out and 'Черновик' not in out, out

        # 3. карточка рейса
        await send(callback_update('trip:t1'), 'Свободно 20 из 20')
        # 4. схема мест
        await send(callback_update('book:t1'), 'Выберите свободное место')
        # 5. выбираем место
        await send(callback_update('seat:t1:5'), 'Кому оформляем')
        # 6. пассажир
        await send(text_update('Азиз Каримов'), 'Телефон для связи')
        # 7. телефон контактом
        await send(contact_update('+998901234567'), 'Email')
        # 8. email пропускаем
        await send(callback_update('mail:skip'), 'обед')
        # 9. обед — итог 250000 + 990 + 100000
        out = await send(callback_update('lunch:1'), 'К оплате: 350 990 сум')
        assert 'Подтвердить и оплатить' in out, out

        # 10. подтверждение → бронь и меню оплаты
        await send(callback_update('bk:ok'), 'Осталось оплатить')
        assert len(db.bookings) == 1, db.bookings
        booking = next(iter(db.bookings.values()))
        assert booking['status'] == 'Новая' and booking['price'] == 350990, booking
        assert booking['phone'] == '+998901234567', booking

        # 11. перевод на карту
        await send(callback_update(f"pay:{booking['id']}:card"), 'Пришлите сюда скриншот чека')
        assert db.payments[booking['id']]['status'] == 'pending', db.payments

        # 12. чек ушёл админу (группа не настроена — ЛС), покупатель ждёт ~2 минуты
        await send(photo_update(), 'Чек получен')
        admin_photo = [m for m in bot.session.sent if type(m).__name__ == 'SendPhoto'
                       and getattr(m, 'chat_id', None) == USER_ID]
        assert admin_photo and 'Чек' in (admin_photo[-1].caption or ''), 'чек админу не ушёл'

        # 13. админ подтверждает
        await send(callback_update(f"adm:ok:{booking['id']}"), 'Оплата подтверждена')
        assert db.bookings[booking['id']]['status'] == 'Оплачена', db.bookings
        assert db.payments[booking['id']]['status'] == 'paid', db.payments
        assert db.bookings[booking['id']]['qr_token'].startswith(f"{booking['id']}."), 'нет qr_token'
        tickets = [m for m in bot.session.sent[-6:]
                   if type(m).__name__ in ('SendMessage', 'SendPhoto')]
        assert any('BLD' in (getattr(m, 'text', None) or '') or
                   'QR' in (getattr(m, 'caption', None) or '') for m in tickets), 'билет не ушёл'

        # 14. проверка статуса
        await send(callback_update(f"check:{booking['id']}"), 'Оплачено')

        # 15. админка
        await send(text_update('/admin'), 'Панель TEPPA')
        await send(callback_update('adm:stats'), 'Броней')

        print('Шаги сценария:')
        print('\n'.join(log))
        print(f'\n{len(log)} шагов — сценарий прошёл, бот отвечает на каждом шаге')
    finally:
        await runtime.shutdown()


def main() -> None:
    import asyncio

    asyncio.run(run())


if __name__ == '__main__':
    main()
