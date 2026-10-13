"""Доступ к Supabase (PostgREST) по service_role — тот же проект, что у сайта.

Клиентские ключи не используются: брони и платежи — серверные данные
(таблица payments вообще доступна только service_role). Все методы асинхронные.
"""

from __future__ import annotations

from datetime import date as date_cls, datetime, timezone
from typing import Any

import httpx


class SupabaseError(RuntimeError):
    """Ошибка запроса к Supabase."""


class SeatConflict(SupabaseError):
    """Место уже занято: уникальный индекс bookings_active_seat_uniq."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_iso() -> str:
    return date_cls.today().isoformat()


class Supabase:
    def __init__(self, url: str, service_key: str):
        self._base = url.rstrip('/')
        self._client = httpx.AsyncClient(
            base_url=f'{self._base}/rest/v1',
            headers={
                'apikey': service_key,
                'Authorization': f'Bearer {service_key}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            },
            timeout=20.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method, path, params=params, json=json, headers=headers
            )
        except httpx.HTTPError as exc:
            raise SupabaseError(f'Supabase недоступна: {exc}') from exc

        if response.status_code == 409:
            raise SeatConflict('Место уже забронировано')
        if response.status_code >= 400:
            detail = response.text[:300]
            raise SupabaseError(f'Supabase {response.status_code}: {detail}')

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    # ---------- Рейсы ----------

    async def upcoming_trips(self) -> list[dict[str, Any]]:
        """Опубликованные рейсы, которые ещё не ушли (как на сайте)."""
        rows = await self._request(
            'GET',
            '/trips',
            params={
                'status': 'eq.published',
                'date': f'gte.{today_iso()}',
                'order': 'date.asc',
            },
        )
        return rows or []

    async def trip(self, trip_id: str) -> dict[str, Any] | None:
        rows = await self._request('GET', '/trips', params={'id': f'eq.{trip_id}'})
        return (rows or [None])[0]

    # ---------- Брони ----------

    async def booking(self, booking_id: str) -> dict[str, Any] | None:
        rows = await self._request('GET', '/bookings', params={'id': f'eq.{booking_id}'})
        return (rows or [None])[0]

    async def bookings_by_trip(self, trip_id: str) -> list[dict[str, Any]]:
        """Активные (не отменённые) брони рейса — для карты мест."""
        rows = await self._request(
            'GET',
            '/bookings',
            params={'trip_id': f'eq.{trip_id}', 'status': 'neq.Отменена', 'select': 'seat,status'},
        )
        return rows or []

    async def bookings_since(self, since_iso: str) -> list[dict[str, Any]]:
        """Брони с момента — для статистики в админке."""
        rows = await self._request(
            'GET',
            '/bookings',
            params={'created_at': f'gte.{since_iso}', 'select': 'status,price'},
        )
        return rows or []

    async def bookings_by_phone(self, phone: str) -> list[dict[str, Any]]:
        """«Мои брони»: ищем по телефону, как в личном кабинете сайта по email."""
        digits = ''.join(ch for ch in phone if ch.isdigit())
        if not digits:
            return []
        # Фильтруем на стороне БД по последним 9 цифрам (форматы записи разные),
        # затем в Python добиваемся строгого совпадения окончания.
        rows = await self._request(
            'GET',
            '/bookings',
            params={
                'phone': f'ilike.%{digits[-9:]}%',
                'order': 'created_at.desc',
                'limit': '50',
            },
        )
        found = [
            row
            for row in (rows or [])
            if ''.join(ch for ch in (row.get('phone') or '') if ch.isdigit()).endswith(digits[-9:])
        ]
        return found

    async def create_booking(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = await self._request(
            'POST',
            '/bookings',
            json=payload,
            headers={'Prefer': 'return=representation'},
        )
        if not rows:
            raise SupabaseError('Бронь не сохранилась: пустой ответ')
        return rows[0]

    async def set_booking_status(self, booking_id: str, status: str) -> None:
        await self._request(
            'PATCH',
            '/bookings',
            params={'id': f'eq.{booking_id}'},
            json={'status': status, 'updated_at': now_iso()},
        )

    async def set_qr_token(self, booking_id: str, token: str) -> None:
        """Выдаёт QR-токен брони (формат как у сайта: <id>.<32 hex>).

        Пишем только если его ещё нет — повторный вызов ничего не ломает.
        """
        await self._request(
            'PATCH',
            '/bookings',
            params={'id': f'eq.{booking_id}', 'qr_token': 'is.null'},
            json={'qr_token': token, 'updated_at': now_iso()},
        )

    # ---------- Платежи ----------

    async def payment(self, booking_id: str) -> dict[str, Any] | None:
        rows = await self._request('GET', '/payments', params={'booking_id': f'eq.{booking_id}'})
        return (rows or [None])[0]

    async def payment_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        rows = await self._request(
            'GET', '/payments', params={'external_id': f'eq.{external_id}'}
        )
        return (rows or [None])[0]

    async def pending_card_payments(self) -> list[dict[str, Any]]:
        rows = await self._request(
            'GET',
            '/payments',
            params={'provider': 'eq.card', 'status': 'eq.pending', 'order': 'created_at.asc'},
        )
        return rows or []

    async def pending_receipts(self) -> list[dict[str, Any]]:
        """Чеки, ждущие проверки: перевод на карту, скриншот уже пришёл.

        Признак полученного чека — error_code='receipt' (его ставит register_receipt).
        """
        rows = await self._request(
            'GET',
            '/payments',
            params={
                'provider': 'eq.card',
                'status': 'eq.pending',
                'error_code': 'eq.receipt',
                'order': 'created_at.asc',
            },
        )
        return rows or []

    async def save_payment(
        self,
        *,
        booking_id: str,
        provider: str,
        amount: int,
        status: str,
        external_id: str | None = None,
        prepare_id: str | None = None,
        error_code: str | None = None,
        error_note: str | None = None,
    ) -> None:
        """Строка платежа (одна на бронь, PK booking_id — upsert)."""
        payload: dict[str, Any] = {
            'booking_id': booking_id,
            'provider': provider,
            'amount': amount,
            'status': status,
            'updated_at': now_iso(),
        }
        if external_id is not None:
            payload['external_id'] = external_id
        if prepare_id is not None:
            payload['prepare_id'] = prepare_id
        if error_code is not None:
            payload['error_code'] = error_code
        if error_note is not None:
            payload['error_note'] = error_note
        if status == 'pending':
            payload['created_at'] = now_iso()

        await self._request(
            'POST',
            '/payments',
            params={'on_conflict': 'booking_id'},
            json=payload,
            headers={
                'Prefer': 'resolution=merge-duplicates,return=minimal',
            },
        )
