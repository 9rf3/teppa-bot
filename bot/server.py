"""HTTP-сервер вебхуков: Telegram, Payme Merchant API и Click SHOP API.

Отвечает Payme/Click ВСЕГДА HTTP 200 — иначе Payme считает ответ RPC-ошибкой -32400.
Логика разбора запросов живёт в bot.payments.*; здесь только транспорт.

Эндпоинт /telegram/webhook работает только в режиме вебхука (TELEGRAM_WEBHOOK_URL
или RENDER_EXTERNAL_URL заданы) и защищён заголовком X-Telegram-Bot-Api-Secret-Token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any
from urllib.parse import parse_qsl

from aiogram.types import Update
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .payments import click as click_api
from .payments import payme as payme_api

log = logging.getLogger('teppa.server')


def create_app(service: Any, settings: Any, db: Any,
               dispatcher: Any = None, bot: Any = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/health')
    async def health() -> dict[str, Any]:
        return {
            'ok': True,
            'mode': 'webhook' if settings.webhook_mode else 'polling',
            'payme': settings.payme_ready,
            'click': settings.click_ready,
            'card': settings.card_ready,
        }

    if settings.webhook_mode and dispatcher is not None and bot is not None:
        @app.post('/telegram/webhook')
        async def telegram_webhook(request: Request) -> JSONResponse:
            expected = settings.telegram_webhook_secret
            if not expected:
                # Без секрета не принимаем ничего: наружу могут писать только Telegram.
                return JSONResponse({'ok': False, 'error': 'webhook secret is not configured'},
                                    status_code=503)
            got = request.headers.get('x-telegram-bot-api-secret-token', '')
            if not secrets.compare_digest(got, expected):
                return JSONResponse({'ok': False, 'error': 'bad secret'}, status_code=403)
            try:
                update = Update.model_validate_json(await request.body())
            except Exception:  # noqa: BLE001 — это не апдейт Telegram
                return JSONResponse({'ok': False, 'error': 'bad update'}, status_code=400)

            try:
                # Отвечаем после обработки: Telegram повторяет доставку только при не-2xx.
                await asyncio.wait_for(dispatcher.feed_update(bot=bot, update=update), timeout=25)
            except asyncio.TimeoutError:
                log.warning('telegram webhook: обработка дольше 25 с — отвечаем 200')
            except Exception:
                log.exception('telegram webhook: ошибка обработки update %s', update.update_id)
            return JSONResponse({'ok': True})

    @app.post('/payme/webhook')
    async def payme_webhook(request: Request) -> JSONResponse:
        body = (await request.body()).decode('utf-8', 'replace')
        result = await payme_api.handle_rpc(
            raw_body=body,
            authorization=request.headers.get('authorization'),
            secret_key=settings.payme_secret_key,
            load_booking=db.booking,
            load_payment=db.payment,
            load_payment_by_external=db.payment_by_external_id,
            save_pending=db.save_payment,
            mark_paid=service.mark_paid,
            mark_cancelled=service.mark_cancelled,
        )
        # Всегда 200: см. док Payme (иначе транзакция «зависает»).
        return JSONResponse(content=result, status_code=200)

    @app.post('/click/webhook')
    async def click_webhook(request: Request) -> JSONResponse:
        result = await click_api.handle_webhook(
            form=await _form(request),
            service_id=settings.click_service_id,
            secret_key=settings.click_secret_key,
            load_booking=db.booking,
            load_payment=db.payment,
            save_pending=db.save_payment,
            mark_paid=service.mark_paid,
            mark_cancelled=service.mark_cancelled,
            match_amount=_match_amount,
        )
        return JSONResponse(content=result, status_code=200)

    # ---------- Внутренние вызовы админки сайта ----------

    def _internal_status(request: Request) -> int:
        """200 — ок; 503 — секрет не задан; 403 — секрет не совпал."""
        if not settings.internal_secret:
            return 503
        got = request.headers.get('x-internal-secret', '')
        return 200 if secrets.compare_digest(got, settings.internal_secret) else 403

    def _internal_reply(status: int, detail: str) -> JSONResponse:
        return JSONResponse({'ok': False, 'error': detail}, status_code=status)

    @app.post('/internal/approve')
    async def internal_approve(request: Request) -> JSONResponse:
        status = _internal_status(request)
        if status != 200:
            return _internal_reply(status, 'forbidden' if status == 403 else 'not configured')
        body = await _json(request)
        booking_id = str(body.get('booking_id') or '')
        if not booking_id:
            return _internal_reply(400, 'booking_id is required')
        try:
            ok = await service.approve_card(booking_id)
        except Exception:  # noqa: BLE001
            log.exception('internal/approve: ошибка брони %s', booking_id)
            return _internal_reply(502, 'db error')
        return JSONResponse({'ok': bool(ok)})

    @app.post('/internal/reject')
    async def internal_reject(request: Request) -> JSONResponse:
        status = _internal_status(request)
        if status != 200:
            return _internal_reply(status, 'forbidden' if status == 403 else 'not configured')
        body = await _json(request)
        booking_id = str(body.get('booking_id') or '')
        if not booking_id:
            return _internal_reply(400, 'booking_id is required')
        try:
            await service.reject_payment(booking_id, str(body.get('reason') or ''))
        except Exception:  # noqa: BLE001
            log.exception('internal/reject: ошибка брони %s', booking_id)
            return _internal_reply(502, 'db error')
        return JSONResponse({'ok': True})

    return app


async def _json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _form(request: Request) -> dict[str, str]:
    """Click шлёт form-urlencoded; парсим вручную — без python-multipart."""
    body = (await request.body()).decode('utf-8', 'replace')
    content_type = request.headers.get('content-type', '')
    if 'json' in content_type:
        try:
            data = json.loads(body)
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return dict(parse_qsl(body, keep_blank_values=True))


def _match_amount(received: Any, expected_uzs: int) -> int | None:
    from .utils import match_amount

    return match_amount(received, expected_uzs)
