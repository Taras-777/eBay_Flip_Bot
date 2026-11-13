"""
🐢 Журнал повільних дій (у «⚙️ Налаштування»).

Кожне натискання кнопки чи команда, обробка якої тривала довше за SLOW_ACTION_SECONDS
або закінчилась помилкою, записується в базу: коли, яка дія, скільки секунд, помилка.
Так видно, які саме кнопки гальмують, без здогадок. Журнал — лише в базі, без тексту
повідомлень і без особистих даних: лише тип кнопки («📌 Екран товару»), а не її вміст.
"""

import asyncio
import html
import io
import re
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from core.settings import LOCAL_TZ, log
from core.db import (
    action_log_summary,
    api_usage_by_day,
    clear_action_log,
    count_unseen_deals,
    get_action_log,
    get_meta,
    list_watches,
    record_action,
)
from ui.panel import _ack_callback, show_panel

SLOW_ACTION_SECONDS = 1.5

# Зрозумілі назви кнопок (за початком callback_data); решта — як є, без чисел
ACTION_LABELS = {
    "menu:home": "🏠 Меню", "menu:list": "🗂 Категорії товарів", "wgrp": "🗂 Категорія", "fld": "🗂 Папка товару", "fldnew": "➕ Нова папка", "menu:discover": "💡 Що перепродавати",
    "dpage": "💡 Що перепродавати", "menu:stats": "📊 Статистика", "stats": "📊 Статистика «💡»",
    "menu:ebay_account": "⚙️ Налаштування", "menu:refresh_prices": "💰 Оновити ціни (усі)",
    "menu:refresh_usage": "🔄 Оновити запити", "menu:addwatch": "➕ Додати товар",
    "deals": "🔥 Вигідні пропозиції", "dact": "🔥 Дія з пропозицією", "mkd": "📉 Знизили ціну",
    "mact": "📉 Дія зі знижкою", "watch_details": "📌 Екран товару", "configs": "🧩 Усі конфігурації",
    "cfgl": "🔎 Оголошення групи", "view_listings": "🔎 Переглянути оголошення", "lpage": "🔎 Сторінка оголошень",
    "sales": "🛒 Продажі", "schk": "🛒 Перевірити продажі", "recalc_median": "🔄 Оновити ціни товару",
    "dadd": "💡 Додати товар", "dhide": "💡 Приховати", "drefresh": "💡 Оновити аналіз",
    "chcat": "🗂️ Категорії", "reqasp": "🧾 Характеристики", "editw": "✏️ Редагувати товар",
    "undo": "↩️ Скасувати", "auctions": "🔨 Аукціони вкл/викл", "slowlog": "🐢 Журнал", "backup:send": "💾 Резервна копія", "eacc:connect": "🔐 Вхід в eBay",
    "eacc:disconnect": "🔌 Відключити eBay", "mdpct": "📉 Поріг знижки", "soldonly": "💰 Ціна для знахідок",
    "mkdclr": "📉 Очистити знижки", "wmp": "💰 Мін. прибуток товару", "apistat": "📡 Статистика запитів",
    "unk": "🛠 Нерозпізнані", "unkall": "🛠 Нерозпізнані (усі)", "unkexp": "📤 Вивантажити нерозпізнані", "unkm": "🛠 Вказати клас", "unks": "🛠 Зберегти клас", "unkb": "🧩 Зібрати клас", "unkbp": "🧩 Зібрати клас",
}


def action_name(update):
    """Назва дії для журналу: тип кнопки або команда (без чисел і текстів користувача)."""
    if not isinstance(update, Update):
        return "інше"
    if update.callback_query:
        data = update.callback_query.data or ""
        for key in (data, data.split(":")[0] + ":" + (data.split(":")[1] if ":" in data else ""), data.split(":")[0]):
            if key in ACTION_LABELS:
                return ACTION_LABELS[key]
        return re.sub(r"\d+", "N", data.split("|")[0])[:40]
    if update.message and update.message.text:
        text = update.message.text
        return text.split()[0][:30] if text.startswith("/") else "✏️ введений текст"
    return "інше"


def _chat_id(update):
    return update.effective_chat.id if isinstance(update, Update) and update.effective_chat else None


async def save_action(update, seconds, error=None):
    try:
        await asyncio.to_thread(record_action, _chat_id(update), action_name(update), seconds, error)
    except Exception as e:   # журнал не має ламати бота
        log.debug("Не вдалося записати дію в журнал: %s", e)


class TimedApplication(Application):
    """Application, що міряє, скільки триває обробка кожного оновлення (натискання, команди)."""

    async def process_update(self, update):
        start = time.monotonic()
        try:
            await super().process_update(update)
        finally:
            elapsed = time.monotonic() - start
            if elapsed >= SLOW_ACTION_SECONDS and isinstance(update, Update):
                log.info("🐢 Повільна дія «%s»: %.1f с", action_name(update), elapsed)
                await save_action(update, elapsed)


def _when(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%d.%m %H:%M")


def journal_text():
    lines = ["🐢 <b>Журнал повільних дій</b>",
             f"<i>Кнопки й команди, які оброблялись довше за {SLOW_ACTION_SECONDS:g} с або з помилкою.</i>"]
    summary = action_log_summary(days=7)
    if summary:
        lines.append("\n<b>За 7 днів — найбільше гальмують:</b>")
        for r in summary:
            err = f" · ⚠️ помилок {r['errors']}" if r["errors"] else ""
            lines.append(f"• {html.escape(r['action'])} — {r['count']} раз, у середньому {r['avg']:.1f} с, "
                         f"найдовше {r['max']:.1f} с{err}")
    recent = get_action_log(15)
    if recent:
        lines.append("\n<b>Останні:</b>")
        for r in recent:
            err = f" · ⚠️ {html.escape(r['error'][:80])}" if r["error"] else ""
            secs = f"{r['seconds']:.1f} с" if r["seconds"] else "—"
            lines.append(f"{_when(r['at'])} · {html.escape(r['action'])} · {secs}{err}")
    else:
        lines.append("\nПоки порожньо — усе працює швидко 👍")
    return "\n".join(lines)


REPORT_DAYS = 30


def _when_full(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%d.%m %H:%M:%S")


def report_text(days=REPORT_DAYS):
    """Звіт для розробника (файлом): повільні дії й помилки за `days` днів і запити до eBay по днях.
    Без токенів, текстів повідомлень і даних продавців — лише назви кнопок, час і помилки."""
    from core.version import get_version   # тут, щоб не тягнути при імпорті модуля
    since = time.time() - days * 86400
    rows = [r for r in get_action_log(5000) if r["at"] >= since]
    watches = list_watches()
    owner_chats = {w["chat_id"] for w in watches}
    lines = [f"Звіт бота для розробника — {datetime.now(LOCAL_TZ):%Y-%m-%d %H:%M}",
             f"Версія: {get_version()} | товарів: {len(watches)} | нових пропозицій 🔥: "
             f"{sum(count_unseen_deals(c) for c in owner_chats)} | інтервал пошуку: "
             f"{get_meta('check_interval') or '?'} хв | поріг повільної дії: {SLOW_ACTION_SECONDS:g} с",
             "", f"=== Найповільніші дії за {days} днів ==="]
    summary = action_log_summary(days=days, limit=50)
    for r in summary:
        lines.append(f"{r['action']}: {r['count']} раз, середнє {r['avg']:.1f} с, макс {r['max']:.1f} с, "
                     f"помилок {r['errors'] or 0}")
    if not summary:
        lines.append("порожньо")
    errors = [r for r in rows if r["error"]]
    lines += ["", f"=== Помилки ({len(errors)}) ==="]
    lines += [f"{_when_full(r['at'])} | {r['action']} | {r['error']}" for r in errors] or ["немає"]
    lines += ["", f"=== Усі записи ({len(rows)}, найновіші першими) ==="]
    lines += [f"{_when_full(r['at'])} | {r['action']} | {r['seconds'] or 0:.1f} с"
              + (f" | ⚠️ {r['error']}" if r["error"] else "") for r in rows] or ["немає"]
    lines += ["", "=== Запити до eBay по днях (UTC) ==="]
    for day, counts in sorted(api_usage_by_day(14).items()):
        lines.append(f"{day}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return "\n".join(lines), len(rows)


async def slowlog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """slowlog — показати журнал; slowlog:clear — очистити; slowlog:file — звіт для розробника файлом."""
    from ui.access import is_owner
    await _ack_callback(update)
    if not is_owner(update.effective_user.id):
        return
    if update.callback_query.data == "slowlog:clear":
        await asyncio.to_thread(clear_action_log)
    if update.callback_query.data == "slowlog:file":
        text, count = await asyncio.to_thread(report_text)
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=InputFile(io.BytesIO(text.encode("utf-8")), filename=f"zvit_bota_{time.strftime('%Y-%m-%d')}.txt"),
            caption=f"📤 Звіт для розробника: {count} записів за {REPORT_DAYS} днів — повільні дії, помилки й запити "
                    "до eBay. Надішли цей файл розробнику. Токенів і даних продавців тут немає.")
        context.user_data["panel_force_new"] = True   # екран — нижче за файл
    text = await asyncio.to_thread(journal_text)
    rows = [[InlineKeyboardButton("🔄 Оновити", callback_data="slowlog"),
             InlineKeyboardButton("🗑 Очистити", callback_data="slowlog:clear")],
            [InlineKeyboardButton("📤 Звіт для розробника (файл)", callback_data="slowlog:file")],
            [InlineKeyboardButton("◀️ До налаштувань", callback_data="menu:ebay_account")]]
    await show_panel(update, context, text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
