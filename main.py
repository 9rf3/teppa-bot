"""TEPPA bot — точка входа.

Запускает одновременно:
  • приём апдейтов Telegram — long-polling (локально) или вебхук (Render);
  • HTTP-сервер вебхуков Payme/Click (uvicorn) на том же процессе.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from bot import runtime
from bot.config import load_settings
from bot.db import Supabase
from bot.handlers import register_all
from bot.payments.service import PaymentService
from bot.server import create_app

log = logging.getLogger('teppa')


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        stream=sys.stdout,
    )

    settings = load_settings()

    db = Supabase(settings.supabase_url, settings.supabase_service_key)
    service = PaymentService(db, settings)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=None),  # текст без разметки — как на сайте
    )
    dp = Dispatcher(storage=MemoryStorage())
    register_all(dp)
    runtime.bind(bot, settings, db, service)

    app = create_app(service, settings, db, dispatcher=dp, bot=bot)
    server = uvicorn.Server(
        uvicorn.Config(app=app, host=settings.host, port=settings.port, log_level='warning')
    )
    http_task = asyncio.create_task(server.serve())

    try:
        # Ждём, пока сервер начнёт слушать порт: вебхуки нельзя отвечать 404 на старте.
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        if not server.started:
            raise RuntimeError(f'HTTP-сервер не поднялся на {settings.host}:{settings.port}')

        if settings.webhook_mode:
            # Режим Render: Telegram сам присылает апдейты на /telegram/webhook,
            # polling не нужен (и конфликтовал бы с вебхуком: getUpdates → 409).
            url = settings.telegram_webhook_url + '/telegram/webhook'
            if url.startswith('https://'):
                await bot.set_webhook(
                    url=url,
                    secret_token=settings.telegram_webhook_secret,
                    drop_pending_updates=False,
                )
                log.info('Режим WEBHOOK: Telegram шлёт апдейты на %s', url)
            else:
                log.warning('Режим WEBHOOK, но адрес не https (%s) — set_webhook пропущен '
                            '(локальная проверка вебхука)', url)
            log.info('TEPPA bot запущен: http://%s:%s (/health, /payme/webhook, /click/webhook)',
                     settings.host, settings.port)
            await http_task
        else:
            # Локальный режим: снимаем чужой вебхук и опрашиваем Telegram сами.
            await bot.delete_webhook(drop_pending_updates=False)
            log.info('Режим POLLING: вебхуки Payme/Click — http://%s:%s/payme/webhook, /click/webhook',
                     settings.host, settings.port)
            await dp.start_polling(bot)
    finally:
        server.should_exit = True
        await http_task
        await runtime.shutdown()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
