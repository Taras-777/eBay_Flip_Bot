"""
🐢 Журнал повільних дій (у «⚙️ Налаштування»).

Кожне натискання кнопки чи команда, обробка якої тривала довше за SLOW_ACTION_SECONDS
або закінчилась помилкою, записується в базу: коли, яка дія, скільки секунд, помилка.
Так видно, які саме кнопки гальмують, без здогадок. Журнал — лише в базі, без тексту
повідомлень і без особистих даних: лише тип кнопки («📌 Екран товару»), а не її вміст.
"""

import asyncio
import html
import re
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from settings import LOCAL_TZ, log
from db import action_log_summary, clear_action_log, get_action_log, record_action
from panel import _ack_callback, show_panel

SLOW_ACTION_SECONDS = 1.5

# Зрозумілі назви кнопок (за початком callback_data); решта — як є, без чисел
ACTION_LABELS = {
    "menu:home": "🏠 Меню", "menu:list": "📋 Мої товари", "menu:discover": "💡 Що перепродавати",
    "dpage": "💡 Що перепродавати", "menu:stats": "📊 Статистика", "stats": "📊 Статистика «💡»",
    "menu:ebay_account": "⚙️ Налаштування", "menu:refresh_prices": "💰 Оновити ціни (усі)",
    "menu:refresh_usage": "🔄 Оновити запити", "menu:addwatch": "➕ Додати товар",
    "deals": "🔥 Вигідні пропозиції", "dact": "🔥 Дія з пропозицією", "mkd": "📉 Знизили ціну",
    "mact": "📉 Дія зі знижкою", "watch_details": "📌 Екран товару", "configs": "🧩 Усі конфігурації",
    "cfgl": "🔎 Оголошення групи", "view_listings": "🔎 Переглянути оголошення", "lpage": "🔎 Сторінка оголошень",
    "sales": "🛒 Продажі", "schk": "🛒 Перевірити продажі", "recalc_median": "🔄 Оновити ціни товару",
    "dadd": "💡 Додати товар", "dhide": "💡 Приховати", "drefresh": "💡 Оновити аналіз",
    "chcat": "🗂️ Категорії", "reqasp": "🧾 Характеристики", "editw": "✏️ Редагувати товар",
    "undo": "↩️ Скасувати", "slowlog": "🐢 Журнал", "ntc": "🔔 Повідомлення",
    "unk": "🛠 Нерозпізнані", "unkm": "🛠 Вказати клас", "unks": "🛠 Зберегти клас",
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


async def slowlog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """slowlog — показати журнал; slowlog:clear — очистити."""
    from access import is_owner
    await _ack_callback(update)
    if not is_owner(update.effective_user.id):
        return
    if update.callback_query.data == "slowlog:clear":
        await asyncio.to_thread(clear_action_log)
    text = await asyncio.to_thread(journal_text)
    rows = [[InlineKeyboardButton("🔄 Оновити", callback_data="slowlog"),
             InlineKeyboardButton("🗑 Очистити", callback_data="slowlog:clear")],
            [InlineKeyboardButton("◀️ До налаштувань", callback_data="menu:ebay_account")]]
    await show_panel(update, context, text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
