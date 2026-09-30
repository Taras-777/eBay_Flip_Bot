"""
«🔍 Перевірити оголошення».
"""

import asyncio
import html
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from settings import log
from checker import CheckError, check_listing
from db import get_watch
from panel import show_main_menu, show_panel
from access import require_access


# ============================================================
# «🔍 ПЕРЕВІРИТИ ОГОЛОШЕННЯ»
# ============================================================

CHECK_LINK = 0  # окрема коротка розмова: чекаємо посилання


@require_access
async def check_listing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """chkl:<id> — просимо надіслати посилання на оголошення."""
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return ConversationHandler.END
    context.user_data["check_watch_id"] = watch_id
    await show_panel(
        update, context,
        f"🔍 <b>Перевірити оголошення для «{html.escape(watch['label'])}»</b>\n\n"
        "Надішли посилання на оголошення eBay або його номер. У застосунку eBay: "
        "«Поділитися» → «Копіювати посилання».\n\n"
        "Бот перевірить кожен фільтр і скаже, чи бачить він це оголошення, а якщо ні — чому.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "◀️ До товару", callback_data=f"watch_details:{watch_id}")]]),
        parse_mode=ParseMode.HTML,
    )
    return CHECK_LINK


async def check_listing_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    watch_id = context.user_data.get("check_watch_id")
    watch = get_watch(watch_id, update.effective_chat.id) if watch_id else None
    if watch is None:
        context.user_data.pop("check_watch_id", None)
        await show_main_menu(update, context)
        return ConversationHandler.END

    back = [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")]
    await show_panel(update, context, "🔍 ⏳ Перевіряю оголошення на eBay…")
    try:
        title, url, checks, verdict = await asyncio.to_thread(check_listing, watch, update.message.text)
    except CheckError as e:
        await show_panel(update, context, f"⚠️ {html.escape(str(e))}\n\nНадішли інше посилання.",
                         reply_markup=InlineKeyboardMarkup([back]), parse_mode=ParseMode.HTML)
        return CHECK_LINK
    except Exception as e:
        log.exception("Не вдалося перевірити оголошення для watch #%s: %s", watch_id, e)
        await show_panel(update, context, "⚠️ Не вдалося перевірити оголошення. Спробуй ще раз.",
                         reply_markup=InlineKeyboardMarkup([back]))
        return CHECK_LINK

    context.user_data.pop("check_watch_id", None)
    lines = [f"🔍 <b>{html.escape(title)}</b>", ""]
    lines += [f"{mark} {html.escape(text)}" for mark, text in checks]
    lines += ["", f"<b>{html.escape(verdict)}</b>"]
    rows = [
        [InlineKeyboardButton("🔗 Відкрити оголошення", url=url)],
        [InlineKeyboardButton("🔍 Перевірити інше", callback_data=f"chkl:{watch_id}")],
        back,
    ]
    await show_panel(update, context, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
                     parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def check_listing_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Інша кнопка, поки бот чекає посилання, — вийти й виконати її."""
    context.user_data.pop("check_watch_id", None)
    data = update.callback_query.data
    if data.startswith("watch_details:"):
        await watch_details_callback(update, context)
    elif data == "menu:list":
        await cmd_list(update, context)
    elif data == "menu:discover":
        await discover_callback(update, context)
    else:
        await show_main_menu(update, context)
    return ConversationHandler.END


async def check_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("check_watch_id", None)
    await show_main_menu(update, context)
    return ConversationHandler.END


# Імпорти з інших екранів — унизу, щоб модулі могли посилатися один на одного
from screen_watch import (  # noqa: E402
    cmd_list,
    watch_details_callback,
)
from screen_discover import (  # noqa: E402
    discover_callback,
)
