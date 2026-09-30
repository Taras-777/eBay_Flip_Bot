"""
«⚙️ Налаштування» (лише власник): вхід в акаунт eBay для перевірки через
Trading API, чи зниклі оголошення справді продали.
"""

import asyncio
import html
import os
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from settings import LOCAL_TZ, TRADING_DAILY_BUDGET, is_owner, log
from db import get_drop_pct, sold_check_stats
from ebay_api import trading_calls_today
from deal_check import trading_breakdown_lines
from ebay_user import (
    UserAuthError,
    connection_info,
    consent_url,
    disconnect,
    exchange_code,
    extract_code,
    is_configured,
    is_connected,
)
from panel import _ack_callback, show_main_menu, show_panel
from backup import backups_summary, make_backup

EBAY_CODE = 0  # чекаємо адресу сторінки після входу в eBay

BACK_ROW = [InlineKeyboardButton("◀️ Меню", callback_data="menu:home")]

WHY_TEXT = (
    "Навіщо: коли оголошення зникає з eBay, бот спитає в eBay, чи його справді "
    "<b>продали</b>, чи продавець просто <b>зняв</b>. Ціна продажу рахуватиметься "
    "лише за справжніми продажами."
)


def _date(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%d.%m.%Y") if ts else "?"


def _not_configured_text():
    return (
        "🔐 <b>Акаунт eBay</b>\n\n" + WHY_TEXT + "\n\n"
        "Спершу потрібне одноразове налаштування:\n"
        "1. developer.ebay.com → <b>User Tokens</b> → <b>Get a Token from eBay via Your "
        "Application</b> → додай <b>eBay Redirect URL</b> (Production).\n"
        "2. Скопіюй його назву (<b>RuName</b>) і впиши на сервері в <code>.env</code>:\n"
        "<code>EBAY_RUNAME=…</code>\n"
        "3. Перезапусти бота (⬆️ Оновити з GitHub в адмін-боті)."
    )


def _connected_text():
    connected_at, expires_at = connection_info()
    stats = sold_check_stats()
    lines = [
        "🔐 <b>Акаунт eBay</b> — ✅ підключено",
        f"з {_date(connected_at)}, вхід діє до {_date(expires_at)}",
        "",
        "<b>Зниклі оголошення за 7 днів:</b>",
        f"✅ продано: {stats.get('sold', 0)}",
        f"🚫 знято без продажу: {stats.get('unsold', 0)}",
        f"↩️ ще продаються: {stats.get('active', 0)}",
        f"❔ eBay не відповів: {stats.get('unknown', 0)}",
        f"⏳ чекають перевірки: {stats.get('pending', 0)}",
        "",
        f"📡 Запитів Trading API сьогодні: {trading_calls_today()[0]} / {TRADING_DAILY_BUDGET}",
        *trading_breakdown_lines(),
    ]
    return "\n".join(lines)


def _login_text(note=""):
    return (
        (f"{note}\n\n" if note else "") +
        "🔐 <b>Акаунт eBay</b> — вхід\n\n" + WHY_TEXT + "\n\n"
        "1. Натисни «🔗 Увійти в eBay» і увійди своїм акаунтом.\n"
        "2. Погодься на доступ (<b>Zustimmen / Agree</b>).\n"
        "3. Відкриється сторінка eBay про успішний вхід — <b>скопіюй її адресу</b> з "
        "адресного рядка і надішли сюди. Поспіши: код діє 5 хвилин.\n\n"
        "<i>Пароля бот не бачить — лише одноразовий код з адреси сторінки.</i>"
    )


def _login_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Увійти в eBay", url=consent_url())], BACK_ROW])


def _settings_text(note=""):
    """Екран «⚙️ Налаштування»: акаунт eBay і резервні копії."""
    parts = [note] if note else []
    parts.append("⚙️ <b>Налаштування</b>")
    if not is_configured():
        parts.append(_not_configured_text())
    elif is_connected():
        parts.append(_connected_text())
    else:
        parts.append("🔐 <b>Акаунт eBay</b> — ❌ не підключено\n\n" + WHY_TEXT)
    parts.append(backups_summary())
    return "\n\n".join(parts)


def _settings_keyboard(chat_id=None):
    rows = []
    if is_configured():
        if is_connected():
            rows.append([InlineKeyboardButton("🔄 Увійти в eBay заново", callback_data="eacc:connect"),
                         InlineKeyboardButton("🔌 Відключити", callback_data="eacc:disconnect")])
        else:
            rows.append([InlineKeyboardButton("🔐 Підключити акаунт eBay", callback_data="eacc:connect")])
    if chat_id is not None:
        rows.append([InlineKeyboardButton(f"📉 Поріг знижки: {get_drop_pct(chat_id):.0f}%",
                                          callback_data="mdpct:show")])
    rows.append([InlineKeyboardButton("💾 Надіслати резервну копію бази", callback_data="backup:send")])
    rows.append(BACK_ROW)
    return InlineKeyboardMarkup(rows)


async def show_settings(update, context, note=""):
    await show_panel(update, context, _settings_text(note), reply_markup=_settings_keyboard(update.effective_chat.id),
                     parse_mode=ParseMode.HTML)


async def ebay_account_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """menu:ebay_account — екран «⚙️ Налаштування»; eacc:connect — вхід в акаунт eBay."""
    await _ack_callback(update)
    if not is_owner(update.effective_user.id):
        await show_main_menu(update, context)
        return ConversationHandler.END
    login = update.callback_query and update.callback_query.data == "eacc:connect"
    if not login or not is_configured():
        await show_settings(update, context)
        return ConversationHandler.END
    await show_panel(update, context, _login_text(), reply_markup=_login_keyboard(), parse_mode=ParseMode.HTML)
    return EBAY_CODE


async def backup_send_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """backup:send — свіжа копія бази файлом (лише власник)."""
    await _ack_callback(update)
    if not is_owner(update.effective_user.id):
        return
    await show_panel(update, context, "💾 ⏳ Роблю резервну копію бази…")
    try:
        path = await asyncio.to_thread(make_backup)
        with open(path, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=InputFile(f, filename=os.path.basename(path)),
                caption="💾 Резервна копія бази бота. Тут і вхід в акаунт eBay — нікому не пересилай.\n"
                        "Відновити: розпакуй і поклади як data/ebay_flip_bot.sqlite3 (бот зупинений).",
            )
        note = "✅ Резервну копію надіслано файлом вище."
    except Exception as e:
        log.exception("Не вдалося надіслати резервну копію: %s", e)
        note = "⚠️ Не вдалося зробити резервну копію — деталі в логах."
    context.user_data.pop("panel_message_id", None)   # екран — нижче за файл
    await show_settings(update, context, note=note)


async def ebay_account_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Власник надіслав адресу сторінки після входу — обмінюємо код на токен."""
    code = extract_code(update.message.text)
    if not code:
        note = ("⚠️ У цій адресі немає коду входу." if "isAuthSuccessful=false" not in update.message.text
                else "⚠️ Вхід скасовано на сторінці eBay.")
        await show_panel(update, context, _login_text(note + " Спробуй ще раз."),
                         reply_markup=_login_keyboard(), parse_mode=ParseMode.HTML)
        return EBAY_CODE
    await show_panel(update, context, "🔐 ⏳ Підключаю акаунт eBay…")
    try:
        await asyncio.to_thread(exchange_code, code)
    except UserAuthError as e:
        await show_panel(update, context,
                         _login_text(f"⚠️ eBay не прийняв код ({html.escape(str(e))}). "
                                     "Можливо, минуло більше 5 хвилин — увійди ще раз."),
                         reply_markup=_login_keyboard(), parse_mode=ParseMode.HTML)
        return EBAY_CODE
    except Exception as e:
        log.exception("Не вдалося підключити акаунт eBay: %s", e)
        await show_panel(update, context, _login_text("⚠️ Не вдалося зв'язатися з eBay. Спробуй ще раз."),
                         reply_markup=_login_keyboard(), parse_mode=ParseMode.HTML)
        return EBAY_CODE
    rows = [[InlineKeyboardButton("⚙️ Налаштування", callback_data="menu:ebay_account")], BACK_ROW]
    await show_panel(update, context,
                     "✅ <b>Акаунт eBay підключено.</b>\n\nВідтепер бот перевірятиме кожне зникле "
                     "оголошення: продано чи просто знято. Перші результати з'являться після "
                     "найближчого ринкового сканування (до години).",
                     reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def ebay_account_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Інша кнопка меню, поки бот чекає адресу, — вийти в меню."""
    await _ack_callback(update)
    await show_main_menu(update, context)
    return ConversationHandler.END


async def ebay_account_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)
    return ConversationHandler.END


async def ebay_account_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ack_callback(update)
    if is_owner(update.effective_user.id):
        disconnect()
    await show_settings(update, context,
                        note="🔌 Акаунт eBay відключено. Бот знову оцінює продажі лише за зниклими оголошеннями.")
