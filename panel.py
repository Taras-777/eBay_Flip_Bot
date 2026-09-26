"""
Меню-панель бота: головне меню, клавіатури, показ і перенесення панелі,
фонові сповіщення.
"""

import asyncio

import config
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from settings import is_owner, log
from db import list_users
from ebay_api import api_usage_line, fetch_browse_rate_limit


async def _ack_callback(update: Update):
    """Гасить 'годинник очікування' на inline-кнопці, якщо оновлення —
    натискання кнопки. Безпечно ігнорує помилку, якщо запит уже
    відповіли раніше (наприклад, повторний виклик у ланцюжку)."""
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass


# ============================================================
# ГОЛОВНЕ МЕНЮ + "ПАНЕЛЬ" (усе відбувається в одному повідомленні,
# яке редагується на кожному кроці, а не в потоці нових повідомлень)
# ============================================================

MENU_LABELS = {
    "addwatch": "➕ Додати товар",
    "list": "📦 Мої відстеження",
    "stats": "📊 Статистика",
    "pending": "⏳ Запити на доступ",
    "users": "👥 Користувачі",
    "refresh_usage": "🔄 Оновити запити",
}


MAIN_MENU_TEXT = "📋 <b>Головне меню</b> — обери дію:"


def main_menu_text(user_id=None):
    """Текст головного меню; власник додатково бачить використання eBay API."""
    if user_id is not None and is_owner(user_id):
        try:
            return f"{MAIN_MENU_TEXT}\n\n{api_usage_line()}"
        except Exception as e:
            log.debug("Не вдалося сформувати рядок використання API: %s", e)
    return MAIN_MENU_TEXT


def build_main_menu(user_id: int) -> InlineKeyboardMarkup:
    """Inline-клавіатура, прикріплена до повідомлення в чаті. Рядок
    власника показується лише тоді, коли є що показувати."""
    def btn(action):
        return InlineKeyboardButton(MENU_LABELS[action], callback_data=f"menu:{action}")

    rows = [
        [btn("addwatch")],
        [btn("list")],
    ]
    if is_owner(user_id):
        owner_row = []
        if list_users(status="pending"):
            owner_row.append(btn("pending"))
        if list_users(status="approved"):
            owner_row.append(btn("users"))
        if owner_row:
            rows.append(owner_row)
        rows.append([btn("refresh_usage")])
    return InlineKeyboardMarkup(rows)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Меню", callback_data="menu:home")]])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="menu:home")]])


async def show_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                      reply_markup=None, parse_mode=None):
    """
    Показує вміст у ЄДИНОМУ "панельному" повідомленні для цього чату:
    редагує його на місці, або надсилає нове, якщо редагування
    неможливе. Повідомлення користувача (команди/введений текст)
    видаляються, щоб не висіли в історії чату.
    """
    chat_id = update.effective_chat.id
    _remember_panel(context, text, reply_markup, parse_mode)
    if update.callback_query:
        context.user_data["panel_message_id"] = update.callback_query.message.message_id
    elif update.message:
        try:
            await update.message.delete()
        except Exception as e:
            log.debug("Не вдалося видалити повідомлення користувача: %s", e)

    panel_id = context.user_data.get("panel_message_id")
    if panel_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=panel_id, text=text,
                reply_markup=reply_markup, parse_mode=parse_mode,
            )
            return
        except Exception as e:
            log.debug("Не вдалося відредагувати панель (%s) — надсилаю нову", e)

    msg = await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode
    )
    context.user_data["panel_message_id"] = msg.message_id


def _remember_panel(context, text, reply_markup, parse_mode):
    """Запам'ятовує, що зараз показано в панелі — щоб перенести той самий
    екран униз чату, коли над ним з'являться нові сповіщення."""
    context.user_data["panel_state"] = {"text": text, "markup": reply_markup, "parse_mode": parse_mode}


async def repost_panel(app, chat_id):
    """
    Переносить панель у самий низ чату: надсилає той самий екран новим
    повідомленням і видаляє старе. Викликається після сповіщень бота, щоб
    меню завжди лишалось останнім повідомленням. Нічого не робить, якщо
    користувач ще не відкривав панель після запуску бота.
    """
    user_data = app.user_data.get(chat_id)  # у приватному чаті chat_id == user_id
    if not user_data:
        return
    state = user_data.get("panel_state")
    old_id = user_data.get("panel_message_id")
    if not state or not old_id:
        return
    try:
        msg = await app.bot.send_message(
            chat_id=chat_id, text=state["text"],
            reply_markup=state["markup"], parse_mode=state["parse_mode"],
        )
    except Exception as e:
        log.debug("Не вдалося перенести панель у чаті %s: %s", chat_id, e)
        return
    user_data["panel_message_id"] = msg.message_id
    try:
        await app.bot.delete_message(chat_id=chat_id, message_id=old_id)
    except Exception as e:
        log.debug("Не вдалося видалити стару панель у чаті %s: %s", chat_id, e)


async def notify(app, chat_id, text, reply_markup=None, parse_mode=None):
    """Сповіщення від бота (не у відповідь на дію користувача). Чат
    позначається, щоб після циклу перевірки перенести панель униз."""
    await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    app.bot_data.setdefault("chats_to_repost_panel", set()).add(chat_id)


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await show_panel(update, context, main_menu_text(user_id), reply_markup=build_main_menu(user_id), parse_mode=ParseMode.HTML)


async def menu_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)


async def refresh_usage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«🔄 Оновити запити» — свіжі дані eBay про ліміт (1 службовий запит,
    у ліміт Browse не рахується) і перемальоване головне меню."""
    query_cb = update.callback_query
    if not is_owner(update.effective_user.id):
        await _ack_callback(update)
        await show_main_menu(update, context)
        return
    try:
        await asyncio.to_thread(fetch_browse_rate_limit)
        await query_cb.answer("Оновлено ✅")
    except Exception as e:
        log.warning("Не вдалося оновити дані про ліміт eBay: %s", e)
        try:
            await query_cb.answer("Не вдалося отримати дані eBay, спробуй пізніше", show_alert=True)
        except Exception:
            pass
    await show_main_menu(update, context)


_owner_notice_state = {"message_id": None}


async def refresh_owner_menu(bot):
    """Оновлює ОДНЕ й те саме повідомлення з меню власника, а не плодить нові."""
    if config.OWNER_TELEGRAM_ID == 0:
        return
    kb = build_main_menu(config.OWNER_TELEGRAM_ID)
    msg_id = _owner_notice_state["message_id"]
    if msg_id:
        try:
            await bot.edit_message_text(
                chat_id=config.OWNER_TELEGRAM_ID, message_id=msg_id,
                text=main_menu_text(config.OWNER_TELEGRAM_ID), reply_markup=kb, parse_mode=ParseMode.HTML,
            )
            return
        except Exception as e:
            log.debug("Не вдалося відредагувати меню власника (%s) — надсилаю нове", e)

    try:
        msg = await bot.send_message(
            chat_id=config.OWNER_TELEGRAM_ID,
            text=main_menu_text(config.OWNER_TELEGRAM_ID),
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        _owner_notice_state["message_id"] = msg.message_id
    except Exception as e:
        log.warning("Не вдалося оновити меню власника: %s", e)