"""Старт, глубокая ссылка на оплату, главное меню и «Мои брони»."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from .. import keyboards, runtime, texts
from ..db import SupabaseError
from ..utils import fmt_short, money
from . import payment

router = Router()


class Mine(StatesGroup):
    """Мои брони: ищем по телефону (на сайте — по email)."""

    phone = State()


# ---------- Старт и подкоманды ----------


@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear()
    # aiogram 3: содержимое после /start лежит в command.args, а не в payload.
    payload = (command.args or '').strip()

    if payload.startswith('pay_'):
        # Ссылка с сайта: «Оплатить в Telegram-боте» → /start pay_<bookingId>
        await payment.send_menu(message, payload[4:])
        return

    await message.answer(texts.WELCOME, reply_markup=keyboards.main_menu())


@router.message(Command('help'))
async def on_help(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=keyboards.main_menu())


@router.message(Command('cancel'))
async def on_cancel_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.CANCELLED_BY_USER, reply_markup=keyboards.main_menu())


@router.message(F.text.in_({'❌ Отмена', 'Отмена'}))
async def on_cancel_button(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer(texts.CANCELLED_BY_USER, reply_markup=keyboards.main_menu())


@router.message(F.text == 'ℹ️ Помощь')
async def on_help_button(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=keyboards.main_menu())


# ---------- Мои брони ----------


async def _render_mine(message: Message, phone: str) -> None:
    try:
        rows = await runtime.db.bookings_by_phone(phone)
    except SupabaseError:
        await message.answer('База недоступна, попробуйте позже.')
        return
    if not rows:
        await message.answer(texts.MY_BOOKINGS_EMPTY, reply_markup=keyboards.main_menu())
        return

    lines = ['Ваши брони:', '']
    for row in rows:
        mark = '✅' if row.get('status') == 'Оплачена' else '⏳'
        lines.append(
            f"{mark} {row.get('trip_title') or row.get('direction')} — "
            f"{fmt_short(str(row.get('created_at') or ''))} · место №{row.get('seat')} · "
            f"{row.get('status')} · {money(int(row.get('price') or 0))}"
        )
    await message.answer('\n'.join(lines), reply_markup=keyboards.main_menu())
    await message.answer('Нажмите на бронь, чтобы получить билет или оплатить:',
                         reply_markup=keyboards.my_bookings(rows))


@router.message(Mine.phone, F.contact)
async def on_mine_contact(message: Message, state: FSMContext) -> None:
    phone = message.contact.phone_number or ''
    await state.clear()
    await _render_mine(message, phone)


@router.message(Mine.phone, F.text)
async def on_mine_phone(message: Message, state: FSMContext) -> None:
    phone = message.text.strip()
    if sum(ch.isdigit() for ch in phone) < 9:
        await message.answer(texts.PHONE_INVALID, reply_markup=keyboards.phone_menu())
        return
    await state.clear()
    await _render_mine(message, phone)


@router.message(F.text == '🎫 Мои брони')
async def on_mine(message: Message, state: FSMContext) -> None:
    await state.set_state(Mine.phone)
    await message.answer(
        'Отправьте контакт кнопкой ниже — покажем ваши брони.',
        reply_markup=keyboards.phone_menu(),
    )


@router.callback_query(F.data == 'mine:list')
async def on_mine_list(callback) -> None:  # noqa: ANN001
    await callback.answer()
    await callback.message.answer(
        'Отправьте контакт кнопкой ниже — покажем ваши брони.',
        reply_markup=keyboards.phone_menu(),
    )
