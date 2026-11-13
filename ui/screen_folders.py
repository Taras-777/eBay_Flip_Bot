"""
Папки «🗂 Категорії товарів»: у яку папку покласти товар.

fld:<товар>              — вибір папки (вбудовані, власні, «🤖 Автоматично», «➕ Нова папка»)
fld:<товар>:<ключ>       — покласти товар у папку (ключ: phones / laptops / … / c<id> / auto)
fldnew:<товар>           — «➕ Нова папка»: бот чекає назву повідомленням (діалог folder_conv)

Після додавання товару бот одразу показує цей вибір (папку можна змінити й пізніше:
картка товару → ✏️ Редагувати → 🗂 Папка).
"""

import asyncio
import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from core.db import FOLDER_NAME_MAX, add_folder, drop_empty_folders, get_watch, list_folders, set_watch_folder
from analysis.market import WATCH_GROUPS, auto_watch_group, watch_group
from ui.panel import _ack_callback, show_panel
from ui.access import require_access

FOLDER_NAME = 0   # діалог: чекаємо назву нової папки


def folder_names(chat_id):
    """{ключ: назва з емодзі} — вбудовані папки й власні («📁 Назва») у порядку показу."""
    names = dict(WATCH_GROUPS)
    names.update({f"c{f['id']}": f"📁 {f['name']}" for f in list_folders(chat_id)})
    return names


def custom_folder_ids(chat_id):
    return {f["id"] for f in list_folders(chat_id)}


async def show_folder_choice(update, context, watch, note=""):
    """Екран вибору папки для товару."""
    chat_id = update.effective_chat.id
    names, current, auto = await asyncio.to_thread(
        lambda: (folder_names(chat_id), watch_group(watch), auto_watch_group(watch)))
    manual = watch.get("folder") is not None and watch.get("folder") == current   # обрано вручну (і папка є)
    wid = watch["id"]
    rows = [[InlineKeyboardButton(("✅ " if not manual else "") + f"🤖 Автоматично ({names[auto]})",
                                  callback_data=f"fld:{wid}:auto")]]
    buttons = [InlineKeyboardButton(("✅ " if manual and key == current else "") + name,
                                    callback_data=f"fld:{wid}:{key}") for key, name in names.items()]
    rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("➕ Нова папка", callback_data=f"fldnew:{wid}")])
    rows.append([InlineKeyboardButton("📌 До товару", callback_data=f"watch_details:{wid}")])
    text = (f"{note}\n\n" if note else "") + (
        f"🗂 <b>Папка для «{html.escape(watch['label'])}»</b>: зараз <b>{html.escape(names[current])}</b>"
        + ("" if manual else " (автоматично)") + "\n\n"
        "Обери, де показувати товар у «🗂 Категорії товарів», або створи нову папку. "
        "«🤖 Автоматично» — бот сам визначає за назвою й категорією eBay.")
    await show_panel(update, context, text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)


async def _after_move(update, context, watch, note):
    from ui.screen_watch import _show_watch_details   # тут, щоб не було циклу імпортів
    await asyncio.to_thread(drop_empty_folders, update.effective_chat.id)
    await _show_watch_details(update, context, get_watch(watch["id"], update.effective_chat.id), note=note)


@require_access
async def folder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """fld:<товар> — вибір; fld:<товар>:<ключ> — покласти в папку."""
    await _ack_callback(update)
    parts = update.callback_query.data.split(":")
    chat_id = update.effective_chat.id
    watch = get_watch(int(parts[1]), chat_id)
    if watch is None:
        return
    if len(parts) < 3:
        return await show_folder_choice(update, context, watch)
    key = parts[2]
    names = await asyncio.to_thread(folder_names, chat_id)
    if key != "auto" and key not in names:
        return await show_folder_choice(update, context, watch, note="⚠️ Такої папки вже немає.")
    await asyncio.to_thread(set_watch_folder, watch["id"], None if key == "auto" else key)
    watch = get_watch(watch["id"], chat_id)
    label = names[await asyncio.to_thread(watch_group, watch)]
    await _after_move(update, context, watch, f"🗂 Папка: <b>{html.escape(label)}</b>"
                      + (" (автоматично)" if key == "auto" else ""))


@require_access
async def folder_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """fldnew:<товар> — попросити назву нової папки."""
    await _ack_callback(update)
    watch = get_watch(int(update.callback_query.data.split(":")[1]), update.effective_chat.id)
    if watch is None:
        return ConversationHandler.END
    context.user_data["new_folder_watch"] = watch["id"]
    rows = [[InlineKeyboardButton("◀️ Назад", callback_data=f"fld:{watch['id']}")]]
    await show_panel(update, context,
                     f"➕ <b>Нова папка</b> для «{html.escape(watch['label'])}»\n\n"
                     f"Напиши назву папки (до {FOLDER_NAME_MAX} символів), напр. «Навушники» чи «Планшети».",
                     reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
    return FOLDER_NAME


async def folder_new_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Назва нової папки — створити й покласти туди товар."""
    chat_id = update.effective_chat.id
    watch = get_watch(context.user_data.pop("new_folder_watch", 0), chat_id)
    name = " ".join((update.message.text or "").split())
    if watch is None:
        return ConversationHandler.END
    if not name:
        context.user_data["new_folder_watch"] = watch["id"]
        await show_panel(update, context, "Назва порожня — напиши назву папки текстом.")
        return FOLDER_NAME
    folder_id = await asyncio.to_thread(add_folder, chat_id, name)
    await asyncio.to_thread(set_watch_folder, watch["id"], f"c{folder_id}")
    context.user_data["panel_force_new"] = True   # відповідь — під повідомленням з назвою
    await _after_move(update, context, watch, f"📁 Створено папку <b>{html.escape(name[:FOLDER_NAME_MAX])}</b> "
                                              "і покладено туди товар.")
    return ConversationHandler.END


async def folder_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Інша кнопка замість назви — вийти з діалогу й виконати її."""
    context.user_data.pop("new_folder_watch", None)
    data = update.callback_query.data
    if data.startswith("fld:"):
        await folder_callback(update, context)
    elif data.startswith("watch_details:"):
        from ui.screen_watch import watch_details_callback
        await watch_details_callback(update, context)
    else:
        from ui.panel import show_main_menu
        await show_main_menu(update, context)
    return ConversationHandler.END
