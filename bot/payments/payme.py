"""Payme Merchant API (JSON-RPC 2.0) — приём оплаты и ссылка на кассу.

Документация: https://developer.help.paycom.uz/
Логика повторяет серверную часть сайта (server/utils/payments/payme.ts):

 — ответ ВСЕГДА HTTP 200: иначе Payme считает это RPC-ошибкой -32400;
 — аутентификация HTTP Basic ``Paycom:<secret key>``;
 — суммы приходят в ТИЙИНАХ (1 сум = 100 тийинов);
 — состояние транзакции: 1 — создана, 2 — проведена,
   -1 — отменена до проведения, -2 — отменена после проведения.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

ERROR = {
    'not_post': -32300,
    'parse': -32700,
    'internal': -32400,
    'privilege': -32504,
    'invalid_object': -32600,
    'method_not_found': -32601,
    'invalid_amount': -31001,
    'transaction_not_found': -31003,
    'could_not_cancel': -31007,
    'could_not_perform': -31008,
    # -31050…-31099: неверные данные поля account (в data лежит sub_field).
    'invalid_account': -31050,
}


def _localized(ru: str, en: str | None = None, uz: str | None = None) -> dict[str, str]:
    return {'ru': ru, 'en': en or ru, 'uz': uz or ru}


class RpcError(Exception):
    def __init__(self, code: int, ru: str, en: str | None = None, uz: str | None = None,
                 sub_field: str | None = None):
        super().__init__(ru)
        self.code = code
        self.localized = _localized(ru, en, uz)
        self.sub_field = sub_field


def rpc_error(request_id: Any, error: RpcError) -> dict[str, Any]:
    body: dict[str, Any] = {'code': error.code, 'message': error.localized}
    if error.sub_field:
        body['data'] = {'sub_field': error.sub_field}
    return {'jsonrpc': '2.0', 'id': request_id, 'error': body}


def rpc_result(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {'jsonrpc': '2.0', 'id': request_id, 'result': dict(result)}


def is_authorized(authorization: str | None, secret_key: str) -> bool:
    """HTTP Basic Paycom:<secret key>, сравнение без утечки времени."""
    expected = 'Basic ' + base64.b64encode(f'Paycom:{secret_key}'.encode('utf-8')).decode('ascii')
    if not authorization or not secret_key:
        return False
    return hmac.compare_digest(authorization.strip(), expected)


def build_checkout_url(
    *,
    merchant_id: str,
    booking_id: str,
    amount_uzs: int,
    return_url: str = '',
    test: bool = True,
) -> str:
    """https://checkout.paycom.uz/<base64>, где base64 — строка параметров
    ``m=…;ac.booking_id=…;a=<тийины>;c=…;l=ru`` (как в официальном Python-пакете)."""
    parts = [
        f'm={merchant_id}',
        f'ac.booking_id={booking_id}',
        f'a={int(round(amount_uzs * 100))}',
    ]
    if return_url:
        parts.append(f'c={return_url}')
    parts.append('l=ru')

    payload = ';'.join(parts).encode('utf-8')
    encoded = base64.b64encode(payload).decode('ascii')
    host = 'https://test.paycom.uz' if test else 'https://checkout.paycom.uz'
    return f'{host}/{encoded}'


def decode_checkout_url(url: str) -> str:
    """Вспомогательное: показать параметры ссылки в логах/тестах."""
    encoded = url.rsplit('/', 1)[-1]
    try:
        return base64.b64decode(encoded + '=' * (-len(encoded) % 4)).decode('utf-8')
    except (binascii.Error, UnicodeDecodeError):
        return ''


def _md5(value: str) -> str:
    return hashlib.md5(value.encode('utf-8')).hexdigest()


def state_of(payment: Mapping[str, Any] | None) -> int:
    """Состояние транзакции из статуса платежа в нашей БД."""
    if not payment:
        return 1
    status = payment.get('status')
    if status == 'paid':
        return 2
    if status in ('cancelled', 'failed'):
        return -1
    return 1


def _time_of(value: Any, fallback: int) -> int:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return fallback


def tx_view(payment: Mapping[str, Any] | None) -> dict[str, Any]:
    state = state_of(payment)
    created = _time_of((payment or {}).get('updated_at') or (payment or {}).get('created_at'), _now_ms())
    return {
        'state': state,
        'create_time': created,
        'perform_time': created if state == 2 else 0,
        'cancel_time': created if state == -1 else 0,
        'transaction': (payment or {}).get('external_id') or '',
    }


def _now_ms() -> int:
    return int(time.time() * 1000)


async def handle_rpc(
    *,
    raw_body: str,
    authorization: str | None,
    secret_key: str,
    load_booking: Callable[[str], Awaitable[dict[str, Any] | None]],
    load_payment: Callable[[str], Awaitable[dict[str, Any] | None]],
    load_payment_by_external: Callable[[str], Awaitable[dict[str, Any] | None]],
    save_pending: Callable[..., Awaitable[None]],
    mark_paid: Callable[..., Awaitable[None]],
    mark_cancelled: Callable[[str, str], Awaitable[None]],
) -> dict[str, Any]:
    """Обрабатывает один JSON-RPC вызов Payme."""
    try:
        request = json.loads(raw_body or '')
    except ValueError:
        return rpc_error(
            None, RpcError(ERROR['parse'], 'Parse error', 'Ошибка разбора JSON', 'JSON xatoligi')
        )

    request_id = request.get('id') if isinstance(request, dict) else None
    method = request.get('method', '') if isinstance(request, dict) else ''
    params = request.get('params', {}) if isinstance(request, dict) else {}
    if not isinstance(params, dict):
        params = {}

    if not is_authorized(authorization, secret_key):
        return rpc_error(
            request_id,
            RpcError(
                ERROR['privilege'],
                'Invalid login/password',
                'Неверный логин или пароль',
                'Login yoki parol notoʻgʻri',
            ),
        )

    def amount_of(value: Any) -> int | None:
        return int(value) if isinstance(value, (int, float)) else None

    async def require_booking(account: Any, amount: Any = None) -> dict[str, Any]:
        source = account if isinstance(account, dict) else {}
        booking_id = str(source.get('booking_id') or '').strip()
        if not booking_id:
            raise RpcError(ERROR['invalid_account'], 'Не указан номер бронирования',
                           sub_field='booking_id')
        booking = await load_booking(booking_id)
        if not booking:
            raise RpcError(ERROR['invalid_account'], 'Бронирование не найдено', sub_field='booking_id')

        expected_tiyin = int(round(int(booking.get('price') or 0) * 100))
        got = amount_of(amount)
        if got is not None and got != expected_tiyin:
            raise RpcError(ERROR['invalid_amount'], 'Неверная сумма платежа')
        return booking

    async def find_transaction(transaction_id: str) -> dict[str, Any]:
        if not transaction_id:
            raise RpcError(ERROR['transaction_not_found'], 'Транзакция не найдена', sub_field='id')
        payment = await load_payment_by_external(transaction_id)
        if not payment:
            raise RpcError(ERROR['transaction_not_found'], 'Транзакция не найдена', sub_field='id')
        return payment

    try:
        if method == 'CheckPerformTransaction':
            booking = await require_booking(params.get('account'), params.get('amount'))
            if booking.get('status') == 'Оплачена':
                return rpc_result(request_id, {'allow': False})
            if booking.get('status') == 'Отменена':
                raise RpcError(ERROR['could_not_perform'], 'Бронирование отменено', sub_field='status')
            return rpc_result(request_id, {'allow': True})

        if method == 'CreateTransaction':
            transaction_id = str(params.get('id') or '')
            if not transaction_id:
                raise RpcError(ERROR['invalid_object'], 'Не указан id транзакции')

            booking = await require_booking(params.get('account'), params.get('amount'))
            if booking.get('status') == 'Отменена':
                raise RpcError(ERROR['could_not_perform'], 'Бронирование отменено', sub_field='status')

            existing = await load_payment(booking['id'])
            # Идемпотентность: повторный CreateTransaction с тем же id.
            if existing and existing.get('external_id') == transaction_id and existing.get('status') != 'pending':
                if existing.get('status') == 'paid':
                    return rpc_result(request_id, tx_view(existing))
                raise RpcError(ERROR['could_not_perform'], 'Транзакция не активна', sub_field='state')

            if booking.get('status') == 'Оплачена':
                raise RpcError(ERROR['could_not_perform'], 'Бронирование уже оплачено', sub_field='status')

            await save_pending(
                booking_id=booking['id'],
                provider='payme',
                amount=int(booking['price']),
                status='pending',
                external_id=transaction_id,
            )
            payment = await load_payment(booking['id'])
            return rpc_result(request_id, tx_view(payment))

        if method == 'PerformTransaction':
            transaction_id = str(params.get('id') or '')
            payment = await find_transaction(transaction_id)

            # Повторное подтверждение — успешный ответ, а не ошибка.
            if payment.get('status') == 'paid':
                return rpc_result(request_id, tx_view(payment))
            if payment.get('status') == 'cancelled':
                raise RpcError(ERROR['could_not_perform'], 'Транзакция отменена', sub_field='state')

            await mark_paid(
                booking_id=payment['booking_id'],
                provider='payme',
                external_id=transaction_id,
                note='Payme: транзакция проведена',
            )
            updated = await load_payment(payment['booking_id'])
            return rpc_result(request_id, tx_view(updated))

        if method == 'CancelTransaction':
            transaction_id = str(params.get('id') or '')
            payment = await find_transaction(transaction_id)
            reason = str(params.get('reason') or '')
            if payment.get('status') != 'cancelled':
                await mark_cancelled(
                    payment['booking_id'], f'Payme: {reason}' if reason else 'Payme: отмена'
                )
            updated = await load_payment(payment['booking_id'])
            return rpc_result(request_id, {**tx_view(updated), 'reason': reason})

        if method == 'CheckTransaction':
            payment = await find_transaction(str(params.get('id') or ''))
            return rpc_result(request_id, tx_view(payment))

        return rpc_error(
            request_id,
            RpcError(
                ERROR['method_not_found'],
                'Method not found',
                'Метод не найден',
                'Metod topilmadi',
            ),
        )
    except RpcError as exc:
        return rpc_error(request_id, exc)
    except Exception:  # noqa: BLE001 — Payme ждёт -32400 при любой внутренней ошибке
        import logging

        logging.getLogger('teppa.payme').exception('webhook failed')
        return rpc_error(
            request_id,
            RpcError(
                ERROR['internal'],
                'Internal system error',
                'Внутренняя ошибка',
                'Ichki tizim xatosi',
            ),
        )
