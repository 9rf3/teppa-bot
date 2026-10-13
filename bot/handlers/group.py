"""Группа Telegram: узнаём её ID для ADMIN_GROUP_ID.

Группы Telegram иногда «апгрейдятся» в супергруппы — тогда ID меняется
(например, -5540287732 → -100xxxxxxxxxx с другими цифрами). Чтобы не гадать,
любое сообщение в группе с ботом просто логируется с ID: админ пишет в группе
«id» и вписывает значение в ADMIN_GROUP_ID.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

log = logging.getLogger('teppa.group')

router = Router()

_GROUP_TYPES = {'group', 'supergroup'}


@router.message(F.chat.type.in_(_GROUP_TYPES))
async def note_group(message: Message) -> None:
    chat = message.chat
    log.info('группа: id=%s, название=%r — впишите id в ADMIN_GROUP_ID, '
             'чтобы чеки падали сюда', chat.id, chat.title)
