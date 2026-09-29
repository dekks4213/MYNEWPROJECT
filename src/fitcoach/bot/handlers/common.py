"""Handlers shared by every flow: /cancel works everywhere."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from fitcoach.bot.ui import Fd, main_menu
from fitcoach.i18n import Translator

router = Router(name="common")


@router.message(Command("cancel"))
async def cancel_cmd(message: Message, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await message.answer(tr("cancelled"), reply_markup=main_menu(tr))


@router.callback_query(Fd.filter(F.action == "cancel"))
async def cancel_cb(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    if isinstance(query.message, Message):
        await query.message.answer(tr("cancelled"), reply_markup=main_menu(tr))


def msg(query: CallbackQuery) -> Message:
    assert isinstance(query.message, Message)
    return query.message
