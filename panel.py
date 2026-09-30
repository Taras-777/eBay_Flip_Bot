"""
Меню-панель бота: головне меню, клавіатури, показ і перенесення панелі,
фонові сповіщення.
"""

import asyncio
from datetime import datetime

import config
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from undo import attach_offer, menu_offer, undo_button
from settings import LOCAL_TZ, TRADING_DAILY_BUDGET, is_owner, log
from db import count_unseen_deals, get_last_prices_update, get_scan_summary, list_users, sold_confirmed_today
from ebay_user import is_connected
from ebay_api import api_usage_line, fetch_browse_rate_limit, trading_calls_today
from version import get_version


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

# Панель — меню, а не стаття: прев'ю посилань лише займали б місце
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


MENU_LABELS = {
    "deals": "🔥 Вигідні пропозиції",
    "addwatch": "➕ Додати товар",
    "discover": "💡 Що перепродавати",
    "list": "📦 Мої товари",
    "stats": "📊 Статистика",
    "pending": "⏳ Запити на доступ",
    "users": "👥 Користувачі",
    "refresh_usage": "🔄 Оновити запити",
    "refresh_prices": "💰 Оновити ціни",
    "ebay_account": "⚙️ Налаштування",
}


MAIN_MENU_TEXT = "📋 <b>Головне меню</b> — обери дію:"


def sold_checks_line():
    """Рядок про запити Trading API (перевірки продажів) — коли акаунт eBay підключено
    або сьогодні вже були перевірки. Число — за даними eBay (усі копії бота разом)."""
    count, _ = trading_calls_today()
    if not count and not is_connected():
        return ""
    line = f"🧾 Перевірки продажів сьогодні: <b>{count}</b> / {TRADING_DAILY_BUDGET}"
    sold = sold_confirmed_today()
    return line + (f" (✅ продано: <b>{sold}</b>)" if count else "")


def prices_updated_line(user_id):
    """«💰 Ціни оновлено о 16:33» — коли востаннє рахувались ціни товарів користувача."""
    ts = get_last_prices_update(user_id)
    if not ts:
        return ""
    moment = datetime.fromtimestamp(ts, LOCAL_TZ)
    today = datetime.now(LOCAL_TZ).date()
    if moment.date() == today:
        when = f"сьогодні о {moment:%H:%M}"
    elif (today - moment.date()).days == 1:
        when = f"вчора о {moment:%H:%M}"
    else:
        when = f"{moment:%d.%m} о {moment:%H:%M}"
    line = f"💰 Ціни товарів оновлено: {when}"
    scan = get_scan_summary(user_id)
    if scan:
        line += (f"\n🔎 Останнє сканування: переглянуто <b>{scan['fetched']}</b> оголошень · "
                 f"підійшло <b>{scan['kept']}</b> · нових у базі <b>{scan['new_count']}</b>")
    return line


def main_menu_text(user_id=None):
    """Текст головного меню; власник додатково бачить використання eBay API.
    Внизу — коли оновлювались ціни й версія бота (змінюється сама після оновлення коду)."""
    footer = f"\n\n<i>🏷 Версія {get_version()}</i>"
    prices = ""
    if user_id is not None:
        try:
            line = prices_updated_line(user_id)
            prices = f"\n\n{line}" if line else ""
        except Exception as e:
            log.debug("Не вдалося визначити час оновлення цін: %s", e)
    if user_id is not None and is_owner(user_id):
        try:
            return f"{MAIN_MENU_TEXT}\n\n{api_usage_line(sold_checks_line())}{prices}{footer}"
        except Exception as e:
            log.debug("Не вдалося сформувати рядок використання API: %s", e)
    return MAIN_MENU_TEXT + prices + footer


def build_main_menu(user_id: int) -> InlineKeyboardMarkup:
    """Inline-клавіатура, прикріплена до повідомлення в чаті. Рядок
    власника показується лише тоді, коли є що показувати."""
    def btn(action):
        return InlineKeyboardButton(MENU_LABELS[action], callback_data=f"menu:{action}")

    unseen = count_unseen_deals(user_id)
    deals_label = MENU_LABELS["deals"] + (f" · 🆕 {unseen}" if unseen else "")
    rows = [
        [InlineKeyboardButton(deals_label, callback_data="deals:0")],
        [btn("list")],
        [btn("discover")],
    ]
    offer = menu_offer(user_id)
    if offer:   # щойно щось видалив і пішов у меню — повернути ще можна
        rows.insert(0, [undo_button(offer["id"], offer["label"])])
    if is_owner(user_id):
        owner_row = []
        if list_users(status="pending"):
            owner_row.append(btn("pending"))
        if list_users(status="approved"):
            owner_row.append(btn("users"))
        if owner_row:
            rows.append(owner_row)
        rows.append([btn("refresh_usage"), btn("refresh_prices")])
        rows.append([btn("ebay_account")])
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
    reply_markup = attach_offer(context, reply_markup)   # «↩️ Скасувати» одразу після дії
    _remember_panel(context, text, reply_markup, parse_mode)
    previous_id = context.user_data.get("panel_message_id")
    if update.callback_query:
        # Натиснута кнопка на повідомленні — воно й стає панеллю. Якщо панеллю
        # досі було інше повідомлення, прибираємо його, щоб меню не двоїлось.
        clicked_id = update.callback_query.message.message_id
        if previous_id and previous_id != clicked_id:
            await _delete_quietly(context.bot, chat_id, previous_id)
        context.user_data["panel_message_id"] = clicked_id
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
                reply_markup=reply_markup, parse_mode=parse_mode, link_preview_options=NO_PREVIEW,
            )
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return  # на екрані вже те саме — нове повідомлення не потрібне
            log.debug("Не вдалося відредагувати панель (%s) — надсилаю нову", e)
        except Exception as e:
            log.debug("Не вдалося відредагувати панель (%s) — надсилаю нову", e)

    msg = await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode,
        link_preview_options=NO_PREVIEW,
    )
    context.user_data["panel_message_id"] = msg.message_id
    if panel_id and panel_id != msg.message_id:
        await _delete_quietly(context.bot, chat_id, panel_id)  # стара панель не має лишатись


async def _delete_quietly(bot, chat_id, message_id):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        log.debug("Не вдалося видалити старе повідомлення панелі %s: %s", message_id, e)


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
    if state.get("main_menu"):
        # Головне меню — свіжий текст і кнопки (напр. новий лічильник «🔥 Вигідні пропозиції»)
        state = dict(state, text=main_menu_text(chat_id), markup=build_main_menu(chat_id))
        user_data["panel_state"] = state
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
    context.user_data["panel_state"]["main_menu"] = True  # при перенесенні — перемалювати (лічильник 🔥)


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
