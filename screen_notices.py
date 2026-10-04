"""
«🔔 Повідомлення»: службові сповіщення бота (ринок проаналізовано, ціни падають,
не вдалося порахувати ціни) — тут, а не окремими повідомленнями в чаті.
Кнопка в головному меню з'являється лише тоді, коли повідомлення є.
Сповіщення про нові вигідні пропозиції, як і раніше, приходять у чат.
"""

import asyncio
import html
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from settings import LOCAL_TZ
from db import clear_notices, count_notices, get_notices, get_watch, mark_notices_seen
from panel import _ack_callback, show_main_menu, show_panel
from access import require_access

NOTICES_PER_PAGE = 5


def _when(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%d.%m %H:%M")


def notices_screen(chat_id, page=0):
    """(текст, кнопки, сторінка) і позначає показані як прочитані."""
    total, unseen = count_notices(chat_id)
    pages = max(1, (total + NOTICES_PER_PAGE - 1) // NOTICES_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    rows = get_notices(chat_id, NOTICES_PER_PAGE, page * NOTICES_PER_PAGE)
    head = f"🔔 <b>Повідомлення</b> ({total})" + (f" · сторінка {page + 1} з {pages}" if pages > 1 else "")
    lines = [head]
    buttons, linked = [], set()
    for r in rows:
        new = "🆕 " if r["seen_at"] is None else ""
        lines.append(f"<b>{new}{_when(r['at'])}</b>\n{html.escape(r['text'])}")
        wid = r["watch_id"]
        if wid and wid not in linked:
            watch = get_watch(wid, chat_id)
            if watch:
                linked.add(wid)
                buttons.append([InlineKeyboardButton(f"📊 {watch['label']}"[:60], callback_data=f"watch_details:{wid}")])
    mark_notices_seen([r["id"] for r in rows])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Новіші", callback_data=f"ntc:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Старіші ▶️", callback_data=f"ntc:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🗑 Очистити всі", callback_data="ntc:clear"),
                    InlineKeyboardButton("◀️ Меню", callback_data="menu:home")])
    return "\n\n".join(lines), InlineKeyboardMarkup(buttons)


@require_access
async def notices_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ntc:<сторінка> — список; ntc:clear — очистити все й повернутись у меню."""
    await _ack_callback(update)
    chat_id = update.effective_chat.id
    data = update.callback_query.data
    if data == "ntc:clear":
        await asyncio.to_thread(clear_notices, chat_id)
        await show_main_menu(update, context)
        return
    page = int(data.split(":")[1]) if data.split(":")[1].isdigit() else 0
    if not (await asyncio.to_thread(count_notices, chat_id))[0]:
        await show_main_menu(update, context)   # уже порожньо (напр. очистили з іншого пристрою)
        return
    text, markup = await asyncio.to_thread(notices_screen, chat_id, page)
    await show_panel(update, context, text, reply_markup=markup, parse_mode=ParseMode.HTML)
