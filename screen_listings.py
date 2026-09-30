"""
«🔎 Переглянути оголошення», «❌ Інший товар» / «🙈 Сховати», вивчені слова; кнопки старих сповіщень.
"""

import asyncio
import html
import time
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from settings import MAX_SPEC_LOOKUPS_PER_DEAL_SCAN, log
from textparse import _search_tokens
from learning import hide_item, learned_words_note, reject_and_learn, unlearn_word
from db import (
    get_deal,
    get_deal_owner_chat_id,
    get_watch,
    set_deal_status,
    watch_category_ids,
)
from ebay_api import _watch_search_kwargs, search_in_categories
from market import _annotate_items, _apply_item_filters
from panel import _ack_callback, show_panel
from access import require_access
from screen_common import _listed


LISTINGS_MAX_PAGES = 3   # за одне натискання — до 3 запитів до eBay
LISTINGS_PAGE_SIZE = 10  # оголошень на одній сторінці меню


LISTINGS_CHUNK = 20          # скільки лотів перевіряти за раз (характеристики — паралельно)
LISTINGS_CACHE_SECONDS = 120  # повторне відкриття списку протягом 2 хв — без запитів до eBay


def _new_fetch_state():
    return {"offset": 0, "exhausted": False, "seen": [], "pending": []}


def _fetch_cheapest(watch, fetch, need):
    """
    Наступні need підходящих лотів (від найдешевших, sort=price).

    Найдешевші лоти в категорії часто — інші моделі (PS4, PS3, Portal…), які
    відсіює перевірка назви. Тому eBay гортаємо сторінками по 100 (до
    LISTINGS_MAX_PAGES за раз), а характеристики перевіряємо порціями по
    LISTINGS_CHUNK лише доти, доки не набереться need — непереглянуті лоти
    лишаються в fetch["pending"] для кнопки «➡️ Наступні». fetch змінюється на місці.
    """
    cats = watch_category_ids(watch)
    seen = set(fetch["seen"])
    kept, pages = [], 0
    while len(kept) < need:
        if not fetch["pending"]:
            if fetch["exhausted"] or pages >= LISTINGS_MAX_PAGES:
                break
            stats = {}
            found = search_in_categories(
                cats, limit=100, offset=fetch["offset"], fresh=True, sort="price", stats=stats,
                **_watch_search_kwargs(watch),
            )
            pages += 1
            fetch["offset"] += 100
            if stats.get("raw", 0) < 100 * max(1, len(cats)) or fetch["offset"] >= 9900:
                fetch["exhausted"] = True  # eBay віддав неповну сторінку — далі лотів немає
            found = [it for it in found if it["item_id"] not in seen]
            seen.update(it["item_id"] for it in found)
            found.sort(key=lambda it: it["total_price"])
            fetch["pending"] = found
            continue
        chunk, fetch["pending"] = fetch["pending"][:LISTINGS_CHUNK], fetch["pending"][LISTINGS_CHUNK:]
        _annotate_items(chunk, max_lookups=MAX_SPEC_LOOKUPS_PER_DEAL_SCAN, watch=watch)
        kept.extend(_apply_item_filters(watch, chunk))
    fetch["seen"] = list(seen)
    kept.sort(key=lambda item: item["total_price"])
    return [
        {"item_id": it["item_id"], "title": it["title"], "price": it["total_price"],
         "currency": it.get("currency") or "EUR", "condition": it.get("condition"), "url": it.get("url")}
        for it in kept
    ]


def _has_more(fetch):
    return bool(fetch) and (not fetch["exhausted"] or bool(fetch["pending"]))


@require_access
async def view_listings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    chat_id = update.effective_chat.id
    watch = get_watch(watch_id, chat_id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return

    # Щойно відкритий список — показуємо з пам'яті, без нових запитів до eBay
    cached = context.user_data.get(_listing_state_key(watch_id))
    if cached and cached.get("fetch") and time.time() - cached.get("fetched_at", 0) < LISTINGS_CACHE_SECONDS:
        await _ack_callback(update)
        cached["page"] = 0
        await _render_listing_panel(update, context, watch_id, cached)
        return

    # Одразу показуємо, що бот працює, — запит до eBay може тривати кілька секунд
    await _ack_callback(update)
    await show_panel(update, context, f"🔎 <b>Оголошення для {html.escape(watch['label'])}</b>\n\n"
                     "⏳ Шукаю на eBay…", parse_mode=ParseMode.HTML,
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                         "◀️ До товару", callback_data=f"watch_details:{watch_id}")]]))

    fetch = _new_fetch_state()
    try:
        items = await asyncio.to_thread(_fetch_cheapest, watch, fetch, LISTINGS_PAGE_SIZE)
    except Exception as e:
        log.exception("Не вдалося завантажити оголошення для watch #%s: %s", watch_id, e)
        await show_panel(
            update, context, "⚠️ Не вдалося завантажити оголошення. Спробуй ще раз.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "◀️ До товару", callback_data=f"watch_details:{watch_id}")]]),
        )
        return

    nav_rows = [
        [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")],
        [InlineKeyboardButton("📦 До моїх товарів", callback_data="menu:list")],
        [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
    ]

    if not items:
        await show_panel(
            update,
            context,
            f"🔎 <b>Оголошення для {html.escape(watch['label'])}</b>\n\n"
            f"Підходящих оголошень не знайдено серед ~{fetch['offset']} найдешевших у категорії.\n\n"
            "Найчастіше це інші моделі чи аксесуари, які відсіює перевірка назви, "
            "або оголошення без потрібних характеристик.",
            reply_markup=InlineKeyboardMarkup(nav_rows),
            parse_mode=ParseMode.HTML,
        )
        return

    state = {
        "header": (f"🔎 <b>Оголошення для {html.escape(watch['label'])}</b>\n"
                   f"Від найдешевших · 🕒 {datetime.now().strftime('%H:%M:%S')}"),
        "items": items,
        "page": 0,
        "fetch": fetch,
        "fetched_at": time.time(),
        "nav": [("◀️ До товару", f"watch_details:{watch_id}"), ("📦 До моїх товарів", "menu:list"),
                ("🏠 Меню", "menu:home")],
        "query": watch["query"],
    }
    await _render_listing_panel(update, context, watch_id, state)


# ============================================================
# «🚫 НЕ ТОЙ ТОВАР»
# ============================================================

def _listing_state_key(watch_id):
    return f"listing_view_{watch_id}"


def _short_listing_label(title, query, max_len=26):
    """Коротка назва для кнопки: без слів із назви товару і «Sony/Apple…»,
    щоб лишилось те, чим лоти відрізняються («Slim 825GB Digital Weiß»)."""
    skip = _search_tokens(query) | {"sony", "apple", "samsung", "lenovo", "hp", "dell", "nintendo", "microsoft",
                                     "edition", "konsole", "console", "spielkonsole", "spielekonsole", "videospielkonsole"}
    words = [w for w in (title or "").split() if not (_search_tokens(w) and _search_tokens(w) <= skip)]
    words = [w for w in words if w.strip("-–—|,*!") ]
    label = " ".join(words).strip(" -–—|,") or (title or "")
    return label if len(label) <= max_len else label[:max_len - 1].rstrip() + "…"


async def _render_listing_panel(update, context, watch_id, state, note="", undo_words=()):
    """Список оголошень з кнопками «🔗», «🙈 Сховати» і «❌ Інший товар» для кожного.
    Стан зберігається, щоб після відхилення перемалювати список без
    нового запиту до eBay."""
    context.user_data[_listing_state_key(watch_id)] = state
    items = state["items"]
    last_page = max(0, (len(items) - 1) // LISTINGS_PAGE_SIZE)
    page = min(state.get("page", 0), last_page)
    state["page"] = page
    start = page * LISTINGS_PAGE_SIZE
    shown = items[start:start + LISTINGS_PAGE_SIZE]
    fetch = state.get("fetch")
    more_on_ebay = _has_more(fetch)
    has_next = len(items) > start + LISTINGS_PAGE_SIZE or more_on_ebay

    lines = [state["header"]]
    if items:
        total = f"{len(items)}+" if more_on_ebay else f"{len(items)}"
        lines[0] += f"\nПоказано <b>{start + 1}–{start + len(shown)}</b> з {total}"
    if items:
        lines.append("<i>🙈 Сховати — той товар, але не підходить (пошкодження тощо)\n"
                     "❌ Інший товар — інша модель чи аксесуар</i>")
    if note:
        lines.append(html.escape(note))
    rows = []
    # Гортання — під оголошеннями, одразу над «◀️ До товару»
    page_row = []
    if page > 0:
        page_row.append(InlineKeyboardButton(
            f"⬅️ Попередні {LISTINGS_PAGE_SIZE}", callback_data=f"lpage:{watch_id}:{page - 1}"))
    if has_next:
        page_row.append(InlineKeyboardButton(
            f"➡️ Наступні {LISTINGS_PAGE_SIZE}", callback_data=f"lpage:{watch_id}:{page + 1}"))
    if not items:
        lines.append("Підходящих оголошень не лишилось.")
    for i, it in enumerate(shown, start + 1):
        cond = f" · стан: {html.escape(it['condition'])}" if it.get("condition") else ""
        listed = f"\n📅 виставлено {_listed(it['created_at'])}" if it.get("created_at") else ""
        lines.append(f"<b>{i}. {html.escape(it['title'][:160])}</b>\n"
                     f"💶 {it['price']:.0f} {html.escape(it['currency'])}{cond}{listed}")
        row = []
        if it.get("url"):
            row.append(InlineKeyboardButton(
                f"🔗 {it['price']:.0f}€ · {_short_listing_label(it['title'], state.get('query', ''))}",
                url=it["url"]))
        if it.get("item_id"):
            row.append(InlineKeyboardButton("🙈 Сховати", callback_data=f"hidel:{watch_id}:{i - 1}"))
            row.append(InlineKeyboardButton("❌ Інший товар", callback_data=f"rejl:{watch_id}:{i - 1}"))
        if row:
            rows.append(row)
    for word in undo_words:
        rows.append([InlineKeyboardButton(f"↩️ Не відсіювати «{word}»", callback_data=f"unlw:{watch_id}:{word}")])
    if page_row:
        rows.append(page_row)
    rows.extend([InlineKeyboardButton(text, callback_data=cb)] for text, cb in state["nav"])
    await show_panel(update, context, "\n\n".join(lines),
                     reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)


@require_access
async def listing_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """lpage:<watch_id>:<сторінка> — наступні/попередні 10 оголошень. Якщо
    завантажених лотів не вистачає — догружає наступні сторінки з eBay."""
    query_cb = update.callback_query
    _, watch_id_str, page_str = query_cb.data.split(":")
    watch_id, page = int(watch_id_str), int(page_str)
    watch = get_watch(watch_id, update.effective_chat.id)
    state = context.user_data.get(_listing_state_key(watch_id))
    if watch is None or not state:
        await query_cb.answer("Список застарів — відкрий оголошення знову.", show_alert=True)
        return

    need = (page + 1) * LISTINGS_PAGE_SIZE - len(state["items"])
    fetch = state.get("fetch")
    answered = False
    if need > 0 and _has_more(fetch):
        await query_cb.answer("⏳ Завантажую наступні оголошення…")
        answered = True
        try:
            more = await asyncio.to_thread(_fetch_cheapest, watch, fetch, need)
        except Exception as e:
            log.exception("Не вдалося догрузити оголошення для watch #%s: %s", watch_id, e)
            await _render_listing_panel(update, context, watch_id, state,
                                        note="⚠️ Не вдалося завантажити наступні оголошення. Спробуй ще раз.")
            return
        known = {it["item_id"] for it in state["items"]}
        state["items"] = state["items"] + [it for it in more if it["item_id"] not in known]

    if page * LISTINGS_PAGE_SIZE >= len(state["items"]):
        if not answered:
            await query_cb.answer("Більше підходящих оголошень немає.", show_alert=True)
        if fetch:
            fetch["exhausted"], fetch["pending"] = True, []
        await _render_listing_panel(update, context, watch_id, state)
        return
    if not answered:
        await _ack_callback(update)
    state["page"] = page
    await _render_listing_panel(update, context, watch_id, state)


@require_access
async def reject_listing_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """rejl:<watch_id>:<індекс> — «не той товар», hidel:… — «сховати» зі списку оголошень."""
    query_cb = update.callback_query
    action, watch_id_str, idx_str = query_cb.data.split(":")
    watch_id = int(watch_id_str)
    watch = get_watch(watch_id, update.effective_chat.id)
    state = context.user_data.get(_listing_state_key(watch_id))
    try:
        item = state["items"][int(idx_str)] if state else None
    except (ValueError, IndexError):
        item = None
    if watch is None or item is None:
        await query_cb.answer("Список застарів — відкрий оголошення знову.", show_alert=True)
        return

    if action == "hidel":
        await asyncio.to_thread(hide_item, watch, item["item_id"], item["title"])
        words = []
    else:
        words = await asyncio.to_thread(reject_and_learn, watch, item["item_id"], item["title"])
    remaining = [it for it in state["items"] if it["item_id"] != item["item_id"]]
    if words:
        word_set = set(words)
        remaining = [it for it in remaining if not (_search_tokens(it["title"]) & word_set)]
    state = {**state, "items": remaining}
    await query_cb.answer("🙈 Сховано — більше не показуватиму це оголошення" if action == "hidel"
                          else "❌ Прибрано — більше не враховую це оголошення")
    note = learned_words_note(words) if words else ""
    await _render_listing_panel(update, context, watch_id, state, note=note, undo_words=words)


@require_access
async def reject_deal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """rejd:<deal_id> — «не той товар», hided:<deal_id> — «сховати» зі сповіщення про знахідку."""
    query_cb = update.callback_query
    action, deal_id_str = query_cb.data.split(":")
    deal = get_deal(int(deal_id_str))
    watch = get_watch(deal["watch_id"], update.effective_chat.id) if deal else None
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return

    hidden = action == "hided"
    if hidden:
        await asyncio.to_thread(hide_item, watch, deal["item_id"], deal["title"])
        words = []
    else:
        words = await asyncio.to_thread(reject_and_learn, watch, deal["item_id"], deal["title"])
    set_deal_status(deal["id"], "skipped")
    await query_cb.answer("🙈 Сховано — більше не показуватиму це оголошення" if hidden
                          else "❌ Прибрано — більше не враховую це оголошення")

    old_rows = query_cb.message.reply_markup.inline_keyboard if query_cb.message.reply_markup else []
    this_deal = {f"rejd:{deal['id']}", f"hided:{deal['id']}"}
    grouped = any(b.callback_data in this_deal and "#" in b.text for row in old_rows for b in row)
    if grouped:
        # Кілька знахідок в одному повідомленні — прибираємо лише кнопки цього лота
        rows = [[b for b in row if b.callback_data not in this_deal] for row in old_rows]
        rows = [row for row in rows if row]
        text = query_cb.message.text
    else:
        rows = [[InlineKeyboardButton("◀️ Меню", callback_data="menu:home")]]
        text = (f"{query_cb.message.text}\n\n"
                + ("🙈 Сховано — це оголошення більше не показуватиму" if hidden
                   else "❌ Інший товар — більше не враховую"))
    if words:
        text += "\n\n" + learned_words_note(words)
        rows = [[InlineKeyboardButton(f"↩️ Не відсіювати «{w}»", callback_data=f"unlw:{watch['id']}:{w}")]
                for w in words] + rows
    try:
        await query_cb.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        log.debug("Не вдалося оновити сповіщення після відхилення: %s", e)


@require_access
async def unlearn_word_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """unlw:<watch_id>:<слово> — скасувати автоматично вивчене слово."""
    query_cb = update.callback_query
    _, watch_id_str, word = query_cb.data.split(":", 2)
    ok = await asyncio.to_thread(unlearn_word, int(watch_id_str), update.effective_chat.id, word)
    if not ok:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    await query_cb.answer(f"↩️ «{word}» більше не відсіюється")
    markup = query_cb.message.reply_markup
    if markup:
        rows = [[b for b in row if b.callback_data != query_cb.data] for row in markup.inline_keyboard]
        try:
            await query_cb.edit_message_reply_markup(InlineKeyboardMarkup([r for r in rows if r]))
        except Exception as e:
            log.debug("Не вдалося прибрати кнопку скасування: %s", e)


@require_access
async def deal_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query

    action, deal_id_str = query_cb.data.split(":")
    deal_id = int(deal_id_str)

    owner_chat_id = get_deal_owner_chat_id(deal_id)
    if owner_chat_id != update.effective_chat.id:
        await query_cb.message.reply_text("⛔ Ця пропозиція тобі не належить.")
        return

    status = "bought" if action == "buy" else "skipped"
    set_deal_status(deal_id, status)

    label = "✅ Куплено" if status == "bought" else "❌ Пропущено"
    await query_cb.edit_message_text(f"{query_cb.message.text}\n\n{label}")
