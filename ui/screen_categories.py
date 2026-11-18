"""
«🗂 Категорії товарів» (/list): папки з кількістю товарів і товари однієї папки.
"""

import asyncio
from collections import Counter

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from core.db import (
    list_archived_watches,
    list_watches,
    watch_category_ids,
)
from analysis.market import (
    watch_group,
)
from ui.panel import (
    _ack_callback,
    show_panel,
)
from ui.access import require_access
from ui.style import grid, menu_row, title


@require_access
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """menu:list, /list — «🗂 Категорії товарів»: папки (телефони, ноутбуки…) з кількістю товарів."""
    chat_id = update.effective_chat.id
    watches = list_watches(chat_id=chat_id)
    archived = await asyncio.to_thread(list_archived_watches, chat_id)
    archive_row = ([[InlineKeyboardButton(f"🗄 Архів ({len(archived)})", callback_data="arch:list")]]
                   if archived else [])
    if not watches:
        await show_panel(
            update, context, title("🗂", "Категорії товарів") + "\n\nУ тебе ще немає товарів. Додай перший"
            + (" або поверни з архіву:" if archived else ":"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Додати товар", callback_data="menu:addwatch")],
                *archive_row,
                menu_row(),
            ]), parse_mode=ParseMode.HTML,
        )
        return
    from ui.screen_folders import custom_folder_ids, folder_names   # тут, щоб не було циклу імпортів

    def _groups():
        ids = custom_folder_ids(chat_id)
        return folder_names(chat_id), Counter(watch_group(w, ids) for w in watches)
    names, groups = await asyncio.to_thread(_groups)
    lines = [title("🗂", "Категорії товарів", count=len(watches), subtitle="обери категорію, щоб побачити її товари")]
    rows = grid([InlineKeyboardButton(f"{name} ({groups[key]})", callback_data=f"wgrp:{key}")
                 for key, name in names.items() if groups[key]])
    rows += [[InlineKeyboardButton("➕ Додати товар", callback_data="menu:addwatch")], *archive_row, menu_row()]
    await show_panel(update, context, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
                     parse_mode=ParseMode.HTML)


@require_access
async def watch_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """wgrp:<папка> — товари однієї категорії."""
    await _ack_callback(update)
    from ui.screen_folders import custom_folder_ids, folder_names   # тут, щоб не було циклу імпортів
    key = update.callback_query.data.split(":")[1]
    chat_id = update.effective_chat.id
    names = await asyncio.to_thread(folder_names, chat_id)
    if key not in names:
        return await cmd_list(update, context)
    all_watches = list_watches(chat_id=chat_id)
    ids = await asyncio.to_thread(custom_folder_ids, chat_id)
    watches = await asyncio.to_thread(lambda: [w for w in all_watches if watch_group(w, ids) == key])
    if not watches:
        return await cmd_list(update, context)
    lines = [f"<b>{names[key]}</b> ({len(watches)})", "<i>обери товар, щоб відкрити його</i>"]
    rows = []
    for w in watches:
        no_cat = " ⚠️" if not watch_category_ids(w) else ""
        label = w["label"] if len(w["label"]) <= 40 else w["label"][:39] + "…"
        rows.append([InlineKeyboardButton(f"📌 {label}{no_cat}", callback_data=f"watch_details:{w['id']}")])
    if any(not watch_category_ids(w) for w in watches):
        lines.append("\n⚠️ — не обрано категорію eBay (відкрий товар → ✏️ Редагувати)")
    rows += [[InlineKeyboardButton("➕ Додати товар", callback_data="menu:addwatch")],
             [InlineKeyboardButton("◀️ Категорії", callback_data="menu:list"), *menu_row()]]
    await show_panel(update, context, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
                     parse_mode=ParseMode.HTML)


# Імпорти з інших екранів — унизу, щоб модулі могли посилатися один на одного
