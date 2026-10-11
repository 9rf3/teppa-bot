"""Платёжная ссылка Click + обработка вебхука Click SHOP API.

Протокол повторяет серверную часть сайта (server/utils/payments/click.ts):
покупатель идёт на my.click.uz/services/pay, а результат Click присылает
единым POST (form-urlencoded) на шаг Prepare (action=0) и Complete (action=1).
Подпись — MD5 от склейки полей в строго определённом порядке.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlencode

ERROR = {
    'success': 0,
    'sign_failed': -1,
    'wrong_amount': -2,
    'action_not_found': -3,
    'already_paid': -4,
    'user_not_found': -5,
    'transaction_not_found': -6,
    'update_failed': -7,
    'request_failed': -8,
    'cancelled': -9,
}


def _md5(value: str) -> str:
    return hashlib.md5(value.encode('utf-8')).hexdigest()


def verify_sign(fields: Mapping[str, str], secret_key: str) -> bool:
    """sign_string = md5(click_trans_id + service_id + secret + merchant_trans_id
    + merchant_prepare_id + amount + action + sign_time)."""
    ordered = [
        fields.get('click_trans_id', ''),
        fields.get('service_id', ''),
        secret_key,
        fields.get('merchant_trans_id', ''),
        fields.get('merchant_prepare_id', ''),
        fields.get('amount', ''),
        fields.get('action', ''),
        fields.get('sign_time', ''),
    ]
    expected = _md5(''.join(ordered))
    received = fields.get('sign_string', '')
    return hmac.compare_digest(expected, received)


def build_link(
    *,
    service_id: str,
    merchant_id: str,
    booking_id: str,
    amount_uzs: int,
    return_url: str = '',
) -> str:
    params: dict[str, str] = {
        'service_id': service_id,
        'merchant_id': merchant_id,
        'amount': str(int(round(amount_uzs))),
        'transaction_param': booking_id,
    }
    if return_url:
        params['return_url'] = return_url
    return 'https://my.click.uz/services/pay?' + urlencode(params)


def _fail(code: int, note: str, **extra: Any) -> dict[str, Any]:
    return {'error': code, 'error_note': note, **extra}


async def handle_webhook(
    *,
    form: Mapping[str, str],
    service_id: str,
    secret_key: str,
    load_booking: Callable[[str], Awaitable[dict[str, Any] | None]],
    load_payment: Callable[[str], Awaitable[dict[str, Any] | None]],
    save_pending: Callable[..., Awaitable[None]],
    mark_paid: Callable[..., Awaitable[None]],
    mark_cancelled: Callable[[str, str], Awaitable[None]],
    match_amount: Callable[[Any, int], int | None],
) -> dict[str, Any]:
    """Разбирает Prepare/Complete и возвращает тело ответа Click."""
    click_trans_id = form.get('click_trans_id', '')
    merchant_trans_id = form.get('merchant_trans_id', '')
    merchant_prepare_id = form.get('merchant_prepare_id', '')
    amount = form.get('amount', '')
    action = form.get('action', '')

    signed = verify_sign(
        {
            'click_trans_id': click_trans_id,
            'service_id': form.get('service_id', ''),
            'merchant_trans_id': merchant_trans_id,
            'merchant_prepare_id': merchant_prepare_id,
            'amount': amount,
            'action': action,
            'sign_time': form.get('sign_time', ''),
            'sign_string': form.get('sign_string', ''),
        },
        secret_key,
    )
    if not signed:
        return _fail(ERROR['sign_failed'], 'SIGN CHECK FAILED!', merchant_trans_id=merchant_trans_id)

    if form.get('service_id', '') != service_id:
        return _fail(ERROR['request_failed'], 'service_id not found', merchant_trans_id=merchant_trans_id)

    if action not in ('0', '1'):
        return _fail(ERROR['action_not_found'], 'Action not found', merchant_trans_id=merchant_trans_id)

    booking = await load_booking(merchant_trans_id)
    if not booking:
        return _fail(ERROR['user_not_found'], 'User does not exist', merchant_trans_id=merchant_trans_id)

    payment = await load_payment(booking['id'])

    # Click сообщает об ошибке/отмене отрицательным error уже на Complete.
    reported = form.get('error', '')
    try:
        reported_error = int(reported) if reported != '' else 0
    except ValueError:
        reported_error = 0
    if action == '1' and reported_error < 0:
        await mark_cancelled(booking['id'], f"Click: {form.get('error_note') or reported_error}")
        return _fail(
            reported_error,
            form.get('error_note') or 'Transaction cancelled',
            merchant_trans_id=merchant_trans_id,
        )

    if payment and payment.get('status') == 'paid':
        return _fail(ERROR['already_paid'], 'Already paid', merchant_trans_id=merchant_trans_id)

    if booking.get('status') == 'Отменена':
        return _fail(ERROR['cancelled'], 'Transaction cancelled', merchant_trans_id=merchant_trans_id)

    if (
        action == '1'
        and payment
        and payment.get('prepare_id')
        and merchant_prepare_id
        and merchant_prepare_id != payment['prepare_id']
    ):
        return _fail(ERROR['transaction_not_found'], 'Transaction not found', merchant_trans_id=merchant_trans_id)

    if match_amount(amount, int(booking.get('price') or 0)) is None:
        return _fail(ERROR['wrong_amount'], 'Incorrect parameter amount', merchant_trans_id=merchant_trans_id)

    if action == '0':
        # Подготовка: бронь есть, сумма верна — запоминаем транзакцию.
        await save_pending(
            booking_id=booking['id'],
            provider='click',
            amount=int(booking['price']),
            status='pending',
            external_id=click_trans_id,
            prepare_id=booking['id'],
        )
        return {
            'click_trans_id': click_trans_id,
            'merchant_trans_id': merchant_trans_id,
            'merchant_prepare_id': booking['id'],
            'error': ERROR['success'],
            'error_note': 'success',
        }

    await mark_paid(
        booking_id=booking['id'],
        provider='click',
        external_id=click_trans_id,
        note='Click: оплата подтверждена',
    )

    return {
        'click_trans_id': click_trans_id,
        'merchant_trans_id': merchant_trans_id,
        'merchant_prepare_id': (payment or {}).get('prepare_id') or booking['id'],
        'error': ERROR['success'],
        'error_note': 'success',
    }
