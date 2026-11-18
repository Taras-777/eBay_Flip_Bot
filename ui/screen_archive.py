"""
«🗄 Архів» товарів: «🗄 В архів» у картці товару замість видалення. Товар у архіві не сканується (запити до
eBay не витрачаються), але історія продажів і цін, вивчені слова, сховані оголошення й налаштування лишаються.

arch:list               — список товарів в архіві
arch:<id>               — картка архівного товару
arch:<id>:back          — ↩️ повернути у відстеження
arch:<id>:del           — 🗑 видалити назавжди (питає підтвердження)
arch:<id>:delyes        — видалити назавжди
"""

import asyncio
import html
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from core.settings import LOCAL_TZ, SOLD_LOOKBACK_DAYS
from core.db import (
    delete_archived_watch,
    get_archived_watch,
    get_learned_words,
    get_watch,
    list_archived_watches,
    unarchive_watch,
    watch_obs_summary,
)
from ui.access import require_access
from ui.panel import _ack_callback, show_panel
from ui.style import hint, menu_row, note_line, title


def _date(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%d.%m.%Y") if ts else "?"


def archive_card_text(watch):
    """Що збережено в архівному товарі."""
    obs = watch_obs_summary(watch["id"])
    learned = sum(1 for st in get_learned_words(watch["id"]).values() if st == "excluded")
    lines = [title("🗄", html.escape(watch["label"]), f"в архіві з {_date(watch.get('archived_at'))}"),
             "",
             f"🔎 Пошук на eBay: «{html.escape(watch.get('query') or '')}»",
             f"🛒 Продажів в історії (за {SOLD_LOOKBACK_DAYS} днів): <b>{obs['sold']}</b>"
             + (f" (✅ {obs['confirmed']})" if obs["confirmed"] else "")]
    if watch.get("min_profit") is not None:
        lines.append(f"💰 Свій мін. прибуток: {watch['min_profit']:.0f}€")
    if learned:
        lines.append(f"🧠 Вивчених слів: {learned}")
    lines += ["", hint(
        "Товар не відстежується: запити до eBay на нього не витрачаються, знахідок і сповіщень немає.\n"
        "Збережено: історія продажів і цін, вивчені слова, сховані оголошення, мін. прибуток, категорії й папка.\n"
        f"Продажі старші за {SOLD_LOOKBACK_DAYS} днів бот прибирає з історії, як і в активних товарів, — тож "
        "що довше товар в архіві, то менше в нього історії (налаштування лишаються).\n"
        "↩️ <b>Повернути</b> — товар знову відстежується з усією історією; поточні оголошення бот збере заново.\n"
        "🗑 <b>Видалити назавжди</b> — історія стирається, повернути не вийде.", "ℹ️ Що це означає")]
    return "\n".join(lines)


async def show_archive(update, context, note=""):
    chat_id = update.effective_chat.id
    archived = await asyncio.to_thread(list_archived_watches, chat_id)
    lines = [note_line(note) + title("🗄", "Архів", count=len(archived),
                                     subtitle="товари, які не відстежуються, але зберігають історію й налаштування")]
    if not archived:
        lines.append("✨ В архіві порожньо. Щоб перемістити товар сюди — картка товару → «🗄 В архів».")
    rows = [[InlineKeyboardButton(f"🗄 {w['label'][:40]} · з {_date(w.get('archived_at'))[:5]}",
                                  callback_data=f"arch:{w['id']}")] for w in archived]
    rows.append([InlineKeyboardButton("◀️ Категорії", callback_data="menu:list"), *menu_row()])
    await show_panel(update, context, "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
                     parse_mode=ParseMode.HTML)


@require_access
async def archive_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """arch:list / arch:<id> / arch:<id>:back / arch:<id>:del / arch:<id>:delyes."""
    await _ack_callback(update)
    parts = update.callback_query.data.split(":")
    chat_id = update.effective_chat.id
    if parts[1] == "list" or not parts[1].isdigit():
        return await show_archive(update, context)
    wid = int(parts[1])
    action = parts[2] if len(parts) > 2 else ""
    watch = await asyncio.to_thread(get_archived_watch, wid, chat_id)
    if watch is None:
        return await show_archive(update, context, "⚠️ Цього товару вже немає в архіві.")
    if action == "back":
        await asyncio.to_thread(unarchive_watch, wid, chat_id)
        from ui.screen_watch import _show_watch_details   # тут, щоб не було циклу імпортів
        return await _show_watch_details(
            update, context, get_watch(wid, chat_id),
            note=f"↩️ «{html.escape(watch['label'])}» повернуто з архіву разом з історією. "
                 "Поточні оголошення бот збере за кілька хвилин.")
    if action == "del":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Так, видалити назавжди", callback_data=f"arch:{wid}:delyes")],
            [InlineKeyboardButton("◀️ Ні, лишити в архіві", callback_data=f"arch:{wid}")],
        ])
        return await show_panel(
            update, context,
            f"🗑 <b>Видалити «{html.escape(watch['label'])}» назавжди?</b>\n\n"
            "Історія продажів і цін цього товару зітреться, і повернути її <b>не вийде</b> — ні кнопкою "
            "«↩️ Скасувати», ні з архіву. Якщо колись знову додаси такий товар, ринок збиратиметься з нуля.",
            reply_markup=keyboard, parse_mode=ParseMode.HTML)
    if action == "delyes":
        await asyncio.to_thread(delete_archived_watch, wid, chat_id)
        return await show_archive(update, context, f"🗑 «{html.escape(watch['label'])}» видалено назавжди.")
    text = await asyncio.to_thread(archive_card_text, watch)
    rows = [[InlineKeyboardButton("↩️ Повернути у відстеження", callback_data=f"arch:{wid}:back")],
            [InlineKeyboardButton("🗑 Видалити назавжди", callback_data=f"arch:{wid}:del")],
            [InlineKeyboardButton("◀️ Архів", callback_data="arch:list"), *menu_row()]]
    await show_panel(update, context, text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
