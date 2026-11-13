"""
«🔨 Аукціони» в головному меню: знайдені вигідні аукціони, які ще тривають (кнопка — лише коли такі є).

auc:<сторінка>              — список (по 5), найближчі до кінця першими; ставки оновлюються при відкритті
auc:h:<сторінка>:<№>        — 🙈 прибрати аукціон зі списку (і більше не нагадувати)
"""

import asyncio
import html
import time
from concurrent.futures import ThreadPoolExecutor

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from core.db import hide_auction, list_auctions, update_auction_bid
from analysis.auctions import _is_accessory, _left, _when, current_bid
from ui.panel import _ack_callback, run_with_progress, show_panel
from ui.access import require_access

PER_PAGE = 5
REFRESH_MINUTES = 10   # ставку при відкритті списку перевіряти, якщо перевіряли давніше


def _title(a):
    """Назва аукціону; для знайдених до появи колонки — з тексту першого сповіщення."""
    if a.get("title"):
        return a["title"]
    lines = (a.get("text") or "").split("\n")
    return html.unescape(lines[1]) if len(lines) > 1 else "аукціон"


def drop_accessories(rows):
    """Аукціони-аксесуари («Filter compatibles con DJI Mini 4 Pro»), знайдені до появи фільтра, — сховати.
    Викликати з потоку. Повертає кількість прихованих."""
    removed = 0
    for a in rows:
        if _is_accessory(a):
            hide_auction(a["chat_id"], a["item_id"])
            removed += 1
    return removed


def refresh_bids(rows):
    """Актуальні ставки аукціонів сторінки (давно не перевірені). Завершені й зняті — прибираються.
    Викликати з потоку. Повертає кількість прибраних."""
    stale = [a for a in rows if (a.get("checked_at") or 0) < time.time() - REFRESH_MINUTES * 60]

    def check(a):
        state, bid, bids = current_bid(a["item_id"])
        if state == "gone":
            hide_auction(a["chat_id"], a["item_id"])
            return 1
        if state == "active":
            update_auction_bid(a["chat_id"], a["item_id"], bid, bids)
        return 0
    if not stale:
        return 0
    with ThreadPoolExecutor(max_workers=min(5, len(stale))) as pool:
        return sum(pool.map(check, stale))


def _card(n, a):
    bid = a.get("bid") or 0
    ship = a.get("shipping") or 0
    max_bid = a["buy_limit"] - ship
    room = a["buy_limit"] - (bid + ship)
    bids = f" · ставок: {a['bid_count']}" if a.get("bid_count") is not None else ""
    checked = (time.strftime(" · перевірено о %H:%M", time.localtime(a["checked_at"])) if a.get("checked_at") else "")
    lines = [f"<b>{n}. {html.escape(a['watch_label'])}</b>",
             html.escape(_title(a)[:140]),
             f"💶 Ставка: <b>{bid:.0f}€</b>" + (f" + доставка {ship:.0f}€" if ship else "") + bids + checked,
             (f"🎯 Ставити не більше <b>{max_bid:.0f}€</b> · запас ~{room:.0f}€" if room >= 0
              else f"❌ Ставка вже дорожча за межу ({max_bid:.0f}€) — невигідно"),
             f"⏰ Закінчиться {_when(a['end_at'])} (через {_left(a['end_at'])})"]
    if a.get("url"):
        lines.append(f'<a href="{html.escape(a["url"])}">🔗 Відкрити на eBay</a>')
    return "\n".join(lines)


async def _render(update, context, page=0, note=""):
    chat_id = update.effective_chat.id
    rows = await asyncio.to_thread(list_auctions, chat_id)
    if await asyncio.to_thread(drop_accessories, rows):
        rows = await asyncio.to_thread(list_auctions, chat_id)
    pages = max(1, (len(rows) + PER_PAGE - 1) // PER_PAGE)
    page = min(max(page, 0), pages - 1)
    shown = rows[page * PER_PAGE:(page + 1) * PER_PAGE]
    removed = await run_with_progress(update, context, "🔨 ⏳ Перевіряю актуальні ставки на eBay…",
                                      refresh_bids, shown) if shown else 0
    rows = await asyncio.to_thread(list_auctions, chat_id)
    pages = max(1, (len(rows) + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages - 1)
    shown = rows[page * PER_PAGE:(page + 1) * PER_PAGE]
    head = f"🔨 <b>Аукціони</b> ({len(rows)}) · найближчі до кінця вгорі"
    if pages > 1:
        head += f" · сторінка {page + 1} з {pages}"
    lines = [head]
    if note:
        lines.append(note)
    if removed:
        lines.append(f"🗑 Прибрано завершених чи знятих: {removed}")
    lines.append("<i>Ставок бот не робить — лише підказує межу. За 45 хв до кінця прийде нагадування "
                 "з актуальною ставкою.</i>")
    for n, a in enumerate(shown, page * PER_PAGE + 1):
        lines.append(_card(n, a))
    if not rows:
        lines.append("Зараз вигідних аукціонів немає — бот сповістить, щойно знайде.")
    buttons = [InlineKeyboardButton(f"🙈 {n}", callback_data=f"auc:h:{page}:{n - 1}")
               for n in range(page * PER_PAGE + 1, page * PER_PAGE + len(shown) + 1)]
    kb = [buttons] if buttons else []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Попередні", callback_data=f"auc:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Наступні ▶️", callback_data=f"auc:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:home")])
    await show_panel(update, context, "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(kb),
                     parse_mode=ParseMode.HTML)


@require_access
async def auctions_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ack_callback(update)
    parts = update.callback_query.data.split(":")
    if parts[1] == "h":
        page, index = int(parts[2]), int(parts[3])
        rows = await asyncio.to_thread(list_auctions, update.effective_chat.id)
        if 0 <= index < len(rows):
            await asyncio.to_thread(hide_auction, update.effective_chat.id, rows[index]["item_id"])
        return await _render(update, context, page, note="🙈 Прибрано зі списку — нагадування не буде.")
    return await _render(update, context, int(parts[1]) if parts[1].isdigit() else 0)
