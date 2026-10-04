"""
«↩️ Скасувати: …» — виконати скасування і повернутися на екран, де була дія.
"""

import asyncio
import html

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from settings import log
from db import get_undo, get_watch, mark_undo_done
from panel import _ack_callback, menu_parts, show_panel
from access import require_access
from undo import OFFER_KEY, perform


async def _show_menu_with_note(update, context, note):
    menu_text, menu_kb = await menu_parts(update.effective_user.id)
    await show_panel(update, context, f"{note}\n\n{menu_text}",
                     reply_markup=menu_kb, parse_mode=ParseMode.HTML)
    context.user_data.setdefault("panel_state", {})["main_menu"] = True


async def _return_to_screen(update, context, action, note):
    """Екран, з якого робили дію, — з приміткою про скасування."""
    p, chat_id = action["payload"], update.effective_chat.id
    raw, note = note, html.escape(note, quote=False)
    screen = p.get("screen")
    watch = get_watch(p["watch_id"], chat_id) if p.get("watch_id") else None
    if screen == "deals":
        return await _render_deals(update, context, p.get("page", 0), note=note)
    if screen == "markdowns":
        return await _render_markdowns(update, context, p.get("page", 0), note=note)
    if screen == "users":
        return await _show_users(update, context, note=raw)
    if watch is None:
        return await _show_menu_with_note(update, context, note)
    if screen == "sales":
        return await _show_sales(update, context, watch, p.get("page", 0), note=note)
    if screen == "learned":
        return await _show_learned_words(update, context, watch, note=note)
    if screen == "listings":
        state = context.user_data.get(_listing_state_key(watch["id"]))
        if state:
            items = list(state["items"])
            for idx, item in sorted(p.get("listing_items") or [], key=lambda x: x[0]):
                if all(it["item_id"] != item["item_id"] for it in items):
                    items.insert(min(idx, len(items)), item)
            return await _render_listing_panel(update, context, watch["id"], {**state, "items": items},
                                               note=raw)
    return await _show_watch_details(update, context, watch, note=note)


@require_access
async def undo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """undo:<id> — скасувати дію."""
    query_cb = update.callback_query
    context.user_data.pop(OFFER_KEY, None)
    action = get_undo(int(query_cb.data.split(":")[1]), update.effective_chat.id)
    if action is None or not mark_undo_done(action["id"]):
        await query_cb.answer("Цю дію вже скасовано.", show_alert=True)
        return await _show_menu_with_note(update, context, "↩️ Цю дію вже скасовано.")
    ok, note = await asyncio.to_thread(perform, action)
    if not ok:
        await query_cb.answer(note, show_alert=True)
        return await _show_menu_with_note(update, context, html.escape(note))
    await _ack_callback(update)
    if action["kind"] == "user_delete":
        try:
            await context.bot.send_message(chat_id=action["payload"]["user"]["chat_id"],
                                           text="🔓 Доступ до бота знову відкрито. Натисни /menu.")
        except Exception as e:
            log.warning("Не вдалося сповістити користувача про відновлення доступу: %s", e)
    await _return_to_screen(update, context, action, note)


# Імпорти з інших екранів — унизу, щоб модулі могли посилатися один на одного
from access import _show_users  # noqa: E402
from screen_deals import _render_deals  # noqa: E402
from screen_markdowns import _render_markdowns  # noqa: E402
from screen_listings import _listing_state_key, _render_listing_panel  # noqa: E402
from screen_sales import _show_sales  # noqa: E402
from screen_watch import _show_learned_words, _show_watch_details  # noqa: E402
