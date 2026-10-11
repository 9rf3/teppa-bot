"""Проверка критичных кусков: подписи Payme/Click, суммы, токен QR, удержание.

Запуск:  python tests/test_signatures.py   (или pytest tests/)
База и токен бота НЕ нужны — только чистая логика.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.payments import click as click_api  # noqa: E402
from bot.payments import payme as payme_api  # noqa: E402
from bot.utils import (  # noqa: E402
    create_qr_token,
    hold_deadline,
    match_amount,
    mmss,
    money,
    to_tiyin,
)

SECRET = 'click_secret_key'
BOOKING = {'id': 'BLD-123456789', 'price': 200990, 'status': 'Новая', 'trip_id': 't1'}


# ---------- utils ----------


def test_money_and_tiyin() -> None:
    assert money(200990) == '200 990 сум'
    assert to_tiyin(200990) == 20099000


def test_match_amount() -> None:
    assert match_amount('200990', 200990) == 200990      # сумы
    assert match_amount('20099000', 200990) == 20099000  # тийины
    assert match_amount(200990.0, 200990) == 200990
    assert match_amount('100000', 200990) is None
    assert match_amount(None, 200990) is None
    assert match_amount('abc', 200990) is None


def test_qr_token_format() -> None:
    token = create_qr_token(BOOKING['id'])
    booking_id, secret = token.split('.')
    assert booking_id == BOOKING['id']
    assert len(secret) == 32 and all(c in '0123456789abcdef' for c in secret)
    # формат должен совпадать с сайтом: <bookingId>.<32hex>
    assert token == f"{BOOKING['id']}.{token.split('.', 1)[1]}"


def test_hold_timer() -> None:
    deadline = hold_deadline('2026-10-24T10:00:00+00:00', 12)
    assert str(deadline) == '2026-10-24 10:12:00+00:00'
    from datetime import datetime, timedelta, timezone

    left = deadline - datetime(2026, 10, 24, 10, 4, 22, tzinfo=timezone.utc)
    assert mmss(left) == '07:38'
    assert mmss(timedelta(seconds=-5)) == '00:00'


# ---------- Click ----------


def _click_fields(action: str, **extra: str) -> dict[str, str]:
    fields = {
        'click_trans_id': '900111',
        'service_id': '1234',
        'merchant_trans_id': BOOKING['id'],
        'merchant_prepare_id': '',
        'amount': '200990',
        'action': action,
        'sign_time': '2026-10-24 10:00:00',
    }
    fields.update(extra)
    # Порядок склейки из протокола: click_trans_id + service_id + secret
    # + merchant_trans_id + merchant_prepare_id + amount + action + sign_time
    ordered = [
        fields['click_trans_id'],
        fields['service_id'],
        SECRET,
        fields['merchant_trans_id'],
        fields['merchant_prepare_id'],
        fields['amount'],
        fields['action'],
        fields['sign_time'],
    ]
    fields['sign_string'] = hashlib.md5(''.join(ordered).encode('utf-8')).hexdigest()
    return fields


def test_click_sign() -> None:
    fields = _click_fields('0')
    assert click_api.verify_sign(fields, SECRET)
    fields['sign_string'] = 'deadbeef'
    assert not click_api.verify_sign(fields, SECRET)


def test_click_pay_link() -> None:
    link = click_api.build_link(
        service_id='1234', merchant_id='9999',
        booking_id=BOOKING['id'], amount_uzs=200990,
        return_url='https://teppa.uz/hold?b=BLD-123456789',
    )
    assert link.startswith('https://my.click.uz/services/pay?')
    assert 'transaction_param=BLD-123456789' in link
    assert 'amount=200990' in link


def test_click_webhook_prepare_and_complete() -> None:
    async def scenario() -> None:
        saved: list[dict] = []
        paid: list[dict] = []

        async def load_booking(booking_id: str):
            return BOOKING if booking_id == BOOKING['id'] else None

        async def load_payment(booking_id: str):
            for row in saved:
                if row['booking_id'] == booking_id:
                    return row
            return None

        async def save_pending(**kwargs):
            saved.append(dict(kwargs))

        async def mark_paid(**kwargs):
            paid.append(dict(kwargs))

        async def mark_cancelled(booking_id: str, reason: str):
            saved.append({'booking_id': booking_id, 'status': 'cancelled', 'reason': reason})

        # Prepare
        prepare = _click_fields('0')
        resp = await click_api.handle_webhook(
            form=prepare, service_id='1234', secret_key=SECRET,
            load_booking=load_booking, load_payment=load_payment,
            save_pending=save_pending, mark_paid=mark_paid,
            mark_cancelled=mark_cancelled, match_amount=match_amount,
        )
        assert resp['error'] == 0, resp
        assert saved and saved[0]['provider'] == 'click' and saved[0]['status'] == 'pending'

        # Complete
        complete = _click_fields('1', merchant_prepare_id=BOOKING['id'])
        resp = await click_api.handle_webhook(
            form=complete, service_id='1234', secret_key=SECRET,
            load_booking=load_booking, load_payment=load_payment,
            save_pending=save_pending, mark_paid=mark_paid,
            mark_cancelled=mark_cancelled, match_amount=match_amount,
        )
        assert resp['error'] == 0, resp
        assert paid and paid[0]['booking_id'] == BOOKING['id']

        # Неверная сумма
        wrong = _click_fields('1', merchant_prepare_id=BOOKING['id'], amount='100')
        resp = await click_api.handle_webhook(
            form=wrong, service_id='1234', secret_key=SECRET,
            load_booking=load_booking, load_payment=load_payment,
            save_pending=save_pending, mark_paid=mark_paid,
            mark_cancelled=mark_cancelled, match_amount=match_amount,
        )
        assert resp['error'] == -2, resp

        # Битая подпись
        bad = _click_fields('1', merchant_prepare_id=BOOKING['id'])
        bad['sign_string'] = '0' * 32
        resp = await click_api.handle_webhook(
            form=bad, service_id='1234', secret_key=SECRET,
            load_booking=load_booking, load_payment=load_payment,
            save_pending=save_pending, mark_paid=mark_paid,
            mark_cancelled=mark_cancelled, match_amount=match_amount,
        )
        assert resp['error'] == -1, resp

    asyncio.run(scenario())


# ---------- Payme ----------


def _basic(secret: str) -> str:
    import base64

    return 'Basic ' + base64.b64encode(f'Paycom:{secret}'.encode()).decode()


def test_payme_auth() -> None:
    assert payme_api.is_authorized(_basic(SECRET), SECRET)
    assert not payme_api.is_authorized(_basic('wrong'), SECRET)
    assert not payme_api.is_authorized(None, SECRET)


def test_payme_checkout_url() -> None:
    url = payme_api.build_checkout_url(
        merchant_id='MerchantId', booking_id=BOOKING['id'],
        amount_uzs=200990, return_url='https://teppa.uz/hold?b=BLD-1',
        test=True,
    )
    assert url.startswith('https://test.paycom.uz/')
    decoded = payme_api.decode_checkout_url(url)
    assert 'm=MerchantId' in decoded
    assert 'ac.booking_id=BLD-123456789' in decoded
    assert 'a=20099000' in decoded  # тийины
    assert 'l=ru' in decoded


def test_payme_rpc_flow() -> None:
    async def scenario() -> None:
        payments: dict[str, dict] = {}
        paid: list[dict] = []

        async def load_booking(booking_id: str):
            if booking_id != BOOKING['id']:
                return None
            row = dict(BOOKING)
            if paid:
                row['status'] = 'Оплачена'
            return row

        async def load_payment(booking_id: str):
            return payments.get(booking_id)

        async def load_payment_by_external(external_id: str):
            for row in payments.values():
                if row.get('external_id') == external_id:
                    return row
            return None

        async def save_pending(**kwargs):
            payments[kwargs['booking_id']] = dict(kwargs)

        async def mark_paid(**kwargs):
            paid.append(dict(kwargs))
            payments[kwargs['booking_id']] = {
                **payments.get(kwargs['booking_id'], {}),
                'status': 'paid',
            }

        async def mark_cancelled(booking_id: str, reason: str):
            payments[booking_id] = {**payments.get(booking_id, {}), 'status': 'cancelled'}

        async def rpc(method: str, params: dict, *, secret: str = SECRET) -> dict:
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
            return await payme_api.handle_rpc(
                raw_body=body,
                authorization=_basic(secret),
                secret_key=SECRET,
                load_booking=load_booking,
                load_payment=load_payment,
                load_payment_by_external=load_payment_by_external,
                save_pending=save_pending,
                mark_paid=mark_paid,
                mark_cancelled=mark_cancelled,
            )

        # 1. без авторизации — отказ
        resp = await rpc('CheckPerformTransaction',
                         {'amount': 20099000, 'account': {'booking_id': BOOKING['id']}},
                         secret='nope')
        assert resp['error']['code'] == -32504

        # 2. CheckPerformTransaction — можно платить
        resp = await rpc('CheckPerformTransaction',
                         {'amount': 20099000, 'account': {'booking_id': BOOKING['id']}})
        assert resp['result']['allow'] is True, resp

        # 3. неверная сумма
        resp = await rpc('CheckPerformTransaction',
                         {'amount': 100, 'account': {'booking_id': BOOKING['id']}})
        assert resp['error']['code'] == -31001

        # 4. неизвестная бронь
        resp = await rpc('CheckPerformTransaction',
                         {'amount': 20099000, 'account': {'booking_id': 'BLD-000'}})
        assert resp['error']['code'] == -31050

        # 5. CreateTransaction
        tx_id = '6f1c0f0a' + 'b' * 24
        resp = await rpc('CreateTransaction',
                         {'id': tx_id, 'amount': 20099000,
                          'account': {'booking_id': BOOKING['id']}})
        assert resp['result']['state'] == 1, resp

        # 6. PerformTransaction
        resp = await rpc('PerformTransaction', {'id': tx_id})
        assert resp['result']['state'] == 2, resp
        assert paid and paid[0]['provider'] == 'payme'

        # 7. повтор PerformTransaction — идемпотентно, не ошибка
        resp = await rpc('PerformTransaction', {'id': tx_id})
        assert resp['result']['state'] == 2, resp

        # 8. CheckTransaction
        resp = await rpc('CheckTransaction', {'id': tx_id})
        assert resp['result']['state'] == 2

        # 9. неизвестный метод
        resp = await rpc('UnknownMethod', {})
        assert resp['error']['code'] == -32601

    asyncio.run(scenario())


def main() -> None:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith('test_') and callable(value)
    ]
    for test in tests:
        test()
        print(f'ok  {test.__name__}')
    print(f'\n{len(tests)} тестов — всё зелёное')


if __name__ == '__main__':
    main()
