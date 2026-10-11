"""TEPPA bot — точка входа.

Запускает одновременно:
  • long-polling Telegram-бота (aiogram 3);
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

    # Очищаем вебхук, если переключаемся на polling.
    await bot.delete_webhook(drop_pending_updates=False)

    app = create_app(service, settings, db)
    server = uvicorn.Server(
        uvicorn.Config(app=app, host=settings.host, port=settings.port, log_level='warning')
    )
    http_task = asyncio.create_task(server.serve())

    log.info('TEPPA bot запущен (вебхуки: http://%s:%s/payme/webhook, /click/webhook)',
             settings.host, settings.port)
    try:
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
