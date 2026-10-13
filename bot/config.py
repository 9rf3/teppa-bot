"""Настройки бота: всё из окружения / .env, секреты никуда не логируются."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field

from dotenv import load_dotenv


def _int_tuple(raw: str) -> tuple[int, ...]:
    """'1, 2, 3' -> (1, 2, 3); мусор молча отбрасывается."""
    out: list[int] = []
    for part in raw.replace(';', ',').split(','):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return tuple(out)


def _int_or_none(raw: str) -> int | None:
    """'-1001234567890' -> -1001234567890; пусто/мусор -> None."""
    raw = raw.strip()
    return int(raw) if raw.lstrip('-').isdigit() else None


@dataclass(frozen=True)
class Settings:
    bot_token: str
    bot_username: str
    admin_ids: tuple[int, ...]

    supabase_url: str
    supabase_service_key: str

    site_url: str = ''

    payme_merchant_id: str = ''
    payme_secret_key: str = ''
    payme_test: bool = True

    click_service_id: str = ''
    click_merchant_id: str = ''
    click_secret_key: str = ''

    card_number: str = ''
    card_holder: str = ''

    # Группа Telegram, куда бот пересылает чеки (супергруппа, бот — участник).
    # Пусто → старый путь: чек каждому админу в ЛС.
    admin_group_id: int | None = None

    # Доступ сайта к /internal/* (подтверждение/отклонение чека из админки).
    internal_secret: str = ''
    # Через сколько секунд чек принимается автоматически, если никто не отклонил.
    receipt_seconds: int = 120

    # SMTP для писем покупателю при отклонении. Пусто → письмо не шлётся.
    smtp_host: str = ''
    smtp_port: int = 587
    smtp_user: str = ''
    smtp_pass: str = ''
    smtp_from: str = ''

    hold_minutes: int = 12
    service_fee: int = 990
    lunch_price: int = 100_000

    # Режим вебхука (Render и другой публичный хостинг):
    # пусто — локальный polling; задан — Telegram шлёт апдейты на <url>/telegram/webhook.
    telegram_webhook_url: str = ''
    telegram_webhook_secret: str = ''

    host: str = '0.0.0.0'
    port: int = 8080

    @property
    def webhook_mode(self) -> bool:
        return bool(self.telegram_webhook_url)

    # Запасной способ связать платёж с чатом, если процесс перезапустили.
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def payme_ready(self) -> bool:
        return bool(self.payme_merchant_id and self.payme_secret_key)

    @property
    def click_ready(self) -> bool:
        return bool(self.click_service_id and self.click_merchant_id and self.click_secret_key)

    @property
    def card_ready(self) -> bool:
        return bool(self.card_number)

    @property
    def smtp_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_port)


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv('BOT_TOKEN', '').strip()
    supabase_url = os.getenv('SUPABASE_URL', '').strip()
    service_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '').strip()

    missing = [
        name
        for name, value in (
            ('BOT_TOKEN', token),
            ('SUPABASE_URL', supabase_url),
            ('SUPABASE_SERVICE_ROLE_KEY', service_key),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            'Не заданы обязательные переменные: ' + ', '.join(missing) + '. Скопируйте .env.example в .env и заполните.'
        )

    # Режим вебхука: на Render адрес подставляется автоматически (RENDER_EXTERNAL_URL).
    webhook_url = (
        os.getenv('TELEGRAM_WEBHOOK_URL', '').strip().rstrip('/')
        or os.getenv('RENDER_EXTERNAL_URL', '').strip().rstrip('/')
    )
    webhook_secret = os.getenv('TELEGRAM_WEBHOOK_SECRET', '').strip()
    if webhook_url and not webhook_secret:
        # Секрет обязателен: иначе /telegram/webhook принимал бы апдейты от кого угодно.
        # Генерируется на каждый запуск — при старте он заново уходит в setWebhook.
        webhook_secret = secrets.token_urlsafe(32)

    return Settings(
        bot_token=token,
        bot_username=os.getenv('BOT_USERNAME', '').strip().lstrip('@'),
        admin_ids=_int_tuple(os.getenv('ADMIN_IDS', '')),
        supabase_url=supabase_url,
        supabase_service_key=service_key,
        site_url=os.getenv('SITE_URL', '').strip().rstrip('/'),
        payme_merchant_id=os.getenv('PAYME_MERCHANT_ID', '').strip(),
        payme_secret_key=os.getenv('PAYME_SECRET_KEY', '').strip(),
        payme_test=os.getenv('PAYME_TEST', 'true').strip().lower() in ('1', 'true', 'yes', 'on'),
        click_service_id=os.getenv('CLICK_SERVICE_ID', '').strip(),
        click_merchant_id=os.getenv('CLICK_MERCHANT_ID', '').strip(),
        click_secret_key=os.getenv('CLICK_SECRET_KEY', '').strip(),
        card_number=os.getenv('CARD_NUMBER', '').strip(),
        card_holder=os.getenv('CARD_HOLDER', '').strip() or 'TEPPA',
        admin_group_id=_int_or_none(os.getenv('ADMIN_GROUP_ID', '')),
        internal_secret=os.getenv('INTERNAL_SECRET', '').strip(),
        receipt_seconds=int(os.getenv('RECEIPT_CHECK_SECONDS', '120') or 120),
        smtp_host=os.getenv('SMTP_HOST', '').strip(),
        smtp_port=int(os.getenv('SMTP_PORT', '587') or 587),
        smtp_user=os.getenv('SMTP_USER', '').strip(),
        smtp_pass=os.getenv('SMTP_PASS', '').strip(),
        smtp_from=os.getenv('SMTP_FROM', '').strip() or os.getenv('SMTP_USER', '').strip(),
        hold_minutes=int(os.getenv('HOLD_MINUTES', '12') or 12),
        service_fee=int(os.getenv('SERVICE_FEE', '990') or 990),
        lunch_price=int(os.getenv('LUNCH_PRICE', '100000') or 100_000),
        host=os.getenv('HOST', '0.0.0.0').strip() or '0.0.0.0',
        port=int(os.getenv('PORT', '8080') or 8080),
        telegram_webhook_url=webhook_url,
        telegram_webhook_secret=webhook_secret,
    )
