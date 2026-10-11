"""HTTP-сервер вебхуков: Payme Merchant API и Click SHOP API.

Отвечает ВСЕГДА HTTP 200 — иначе Payme считает ответ RPC-ошибкой -32400.
Логика разбора запросов живёт в bot.payments.*; здесь только транспорт.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .payments import click as click_api
from .payments import payme as payme_api

log = logging.getLogger('teppa.server')


def create_app(service: Any, settings: Any, db: Any) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/health')
    async def health() -> dict[str, Any]:
        return {
            'ok': True,
            'payme': settings.payme_ready,
            'click': settings.click_ready,
            'card': settings.card_ready,
        }

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

    return app


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
