"""Вебхук Telegram (режим Render): секрет, приём апдейта, ответы HTTP.

Запуск:  python tests/test_webhook.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from bot import runtime  # noqa: E402
from bot.handlers import register_all  # noqa: E402
from bot.payments.service import PaymentService  # noqa: E402
from bot.server import create_app  # noqa: E402
from bot.texts import WELCOME  # noqa: E402
from test_flows import FakeDb, FakeSession, settings as base_settings, text_update  # noqa: E402

SECRET = 'test-secret-123'


def build_app(webhook: bool):
    cfg = replace(
        base_settings(),
        telegram_webhook_url='https://teppa-bot.onrender.com' if webhook else '',
        telegram_webhook_secret=SECRET,
    )
    db = FakeDb()
    service = PaymentService(db, cfg)
    bot = Bot(token='42:TEST', session=FakeSession(),
              default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    if webhook:
        # Регистрируем хендлеры только здесь: роутеры — синглтоны и не крепятся дважды.
        # Для проверки режима polling пустого диспетчера достаточно.
        register_all(dp)
    runtime.bind(bot, cfg, db, service)
    return create_app(service, cfg, db, dispatcher=dp, bot=bot), bot


def main() -> None:
    # 1. health сообщает режим
    app, bot = build_app(webhook=True)
    with TestClient(app) as client:
        health = client.get('/health').json()
        assert health['ok'] and health['mode'] == 'webhook', health

        raw = text_update('/start').model_dump_json(by_alias=True, exclude_none=True)
        headers = {'X-Telegram-Bot-Api-Secret-Token': SECRET}

        # 2. неверный секрет / без секрета — отказ до чтения тела
        bad = {'X-Telegram-Bot-Api-Secret-Token': 'wrong'}
        assert client.post('/telegram/webhook', content=raw, headers=bad).status_code == 403
        assert client.post('/telegram/webhook', content=raw).status_code == 403

        # 3. секрет верен, но тело не апдейт
        assert client.post('/telegram/webhook', content='not json',
                           headers=headers).status_code == 400

        # 4. настоящий апдейт → 200, и ответ бота ушёл в сессию
        response = client.post('/telegram/webhook', content=raw, headers=headers)
        assert response.status_code == 200 and response.json() == {'ok': True}, response.text
        sent_texts = [getattr(m, 'text', None) for m in bot.session.sent]
        assert WELCOME in sent_texts, f'бот не ответил на /start: {sent_texts}'

    # 5. в режиме polling (локально) эндпоинта не существует
    app_poll, _ = build_app(webhook=False)
    with TestClient(app_poll) as client:
        assert client.get('/health').json()['mode'] == 'polling'
        assert client.post('/telegram/webhook', content='{}',
                           headers={'X-Telegram-Bot-Api-Secret-Token': SECRET}).status_code == 404

    asyncio.run(runtime.shutdown())
    print('5 проверок — всё зелёное: секрет 403, мусор 400, апдейт 200, polling → 404')


if __name__ == '__main__':
    main()
