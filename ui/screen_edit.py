"""
«✏️ Редагувати» товар: категорії eBay, обов'язкові характеристики, мінімальна ціна (введення значення — діалог edit_conv).
"""

import asyncio
import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from core.settings import (
    MIN_PRICE_SUGGESTION_PCT,
    log,
)
from analysis.textparse import (
    aspects_label,
)
from core.db import (
    encode_required_aspects,
    get_auto_min_price,
    get_market_stats,
    get_required_aspects,
    get_watch,
    get_watch_categories,
    mark_market_stale,
    reset_active_listings,
    update_watch_categories,
    update_watch_min_price,
    update_watch_required_aspect,
    watch_category_ids,
)
from ebay.ebay_api import category_descendants, collapse_categories, mark_parent_categories
from ebay.ebay_api import (
    aspect_options_for_categories,
    get_category_options,
)
from analysis.market import (
    apply_filters_to_history,
)
from ui.panel import (
    refresh_usage_callback,
    _ack_callback,
    run_with_progress,
    show_main_menu,
    show_panel,
)
from ui.access import require_access
from ui.style import title


async def _render_aspect_picker(update, context, watch):
    watch_id = watch["id"]
    options = context.user_data.get(f"asp_options_{watch_id}") or []
    selected = context.user_data.get(f"asp_selected_{watch_id}") or set()
    current = aspects_label(get_required_aspects(watch)) or (
        "авто" if watch.get("require_spec") is None else "без вимоги")
    back_row = [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")]
    await show_panel(
        update, context,
        f"🧾 <b>{html.escape(watch['label'])}: обов'язкові характеристики</b>\n"
        f"Зараз: {html.escape(current, quote=False)}\n\n{ASPECT_CHOICE_TEXT}",
        reply_markup=_aspect_keyboard(watch_id, options, selected, extra_rows=[back_row]),
        parse_mode=ParseMode.HTML,
    )


@require_access
async def required_aspect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «Обов'язкові характеристики» на екрані товару."""
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    back_row = [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")]
    if not watch_category_ids(watch):
        await show_panel(
            update, context,
            "Характеристики залежать від категорії eBay. Спершу обери категорії кнопкою "
            "«🗂️ Категорії».",
            reply_markup=InlineKeyboardMarkup([back_row]),
        )
        return

    await show_panel(update, context, "🔎 Дізнаюсь характеристики категорії в eBay…")
    try:
        options = await asyncio.to_thread(aspect_options_for_categories, watch_category_ids(watch), watch["query"])
    except Exception as e:
        log.warning("Не вдалося отримати характеристики категорії для watch #%s: %s", watch_id, e)
        options = []
    selected = set(get_required_aspects(watch))
    # Уже обрані характеристики лишаються у списку, навіть якщо їх немає серед пропозицій
    known = {o["name"] for o in options}
    options += [{"name": name, "required": False, "usage": ""} for name in selected if name not in known]
    context.user_data[f"asp_options_{watch_id}"] = options
    context.user_data[f"asp_selected_{watch_id}"] = selected
    await _render_aspect_picker(update, context, watch)


@require_access
async def toggle_aspect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    _, watch_id_str, idx_str = query_cb.data.split(":")
    watch_id = int(watch_id_str)
    watch = get_watch(watch_id, update.effective_chat.id)
    options = context.user_data.get(f"asp_options_{watch_id}")
    if watch is None or options is None:
        await query_cb.answer("Список застарів — відкрий його знову.", show_alert=True)
        return
    try:
        name = options[int(idx_str)]["name"]
    except (ValueError, IndexError):
        return
    selected = context.user_data.setdefault(f"asp_selected_{watch_id}", set())
    selected.symmetric_difference_update({name})
    await _render_aspect_picker(update, context, watch)


@require_access
async def save_aspects_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    chat_id = update.effective_chat.id
    if get_watch(watch_id, chat_id) is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    options = context.user_data.get(f"asp_options_{watch_id}") or []
    selected = context.user_data.get(f"asp_selected_{watch_id}") or set()
    ordered = [o["name"] for o in options if o["name"] in selected]  # порядок як у списку
    # Нічого не позначено → автоматичний режим
    update_watch_required_aspect(watch_id, chat_id, encode_required_aspects(ordered), 0 if ordered else None)
    context.user_data.pop(f"asp_options_{watch_id}", None)
    context.user_data.pop(f"asp_selected_{watch_id}", None)
    # Історію не стираємо — прибираємо лише те, що не проходить нові вимоги
    await asyncio.to_thread(apply_filters_to_history, get_watch(watch_id, chat_id))
    await _show_watch_details(update, context, get_watch(watch_id, chat_id))


@require_access
async def set_required_aspect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки «🤖 Авто» і «🚫 Без вимоги»."""
    query_cb = update.callback_query
    _, watch_id_str, choice = query_cb.data.split(":", 2)
    watch_id = int(watch_id_str)
    chat_id = update.effective_chat.id
    if get_watch(watch_id, chat_id) is None or choice not in ("auto", "none"):
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    update_watch_required_aspect(watch_id, chat_id, None, None if choice == "auto" else 0)
    context.user_data.pop(f"asp_options_{watch_id}", None)
    context.user_data.pop(f"asp_selected_{watch_id}", None)
    await asyncio.to_thread(apply_filters_to_history, get_watch(watch_id, chat_id))
    await _show_watch_details(update, context, get_watch(watch_id, chat_id))


@require_access
async def change_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує категорії eBay для вже доданого товару (кнопка «Змінити категорію»)."""
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return

    await show_panel(update, context, "🔎 Шукаю категорії eBay…")
    try:
        options = await asyncio.to_thread(get_category_options, watch["query"], watch["condition_ids"])
    except Exception as e:
        log.warning("Не вдалося отримати категорії для watch #%s: %s", watch_id, e)
        options = []

    back_row = [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")]
    if not options:
        await show_panel(
            update, context,
            "Не вдалося отримати категорії від eBay. Спробуй пізніше.",
            reply_markup=InlineKeyboardMarkup([back_row]),
        )
        return
    # Уже обрані категорії лишаються у списку, навіть якщо зараз їх немає серед пропозицій
    current = get_watch_categories(watch)
    known = {o["id"] for o in options}
    options += [{"id": c["id"], "name": c["name"], "count": 0} for c in current if c["id"] not in known]
    await asyncio.to_thread(mark_parent_categories, options)
    context.user_data[f"cat_options_{watch_id}"] = options
    context.user_data[f"cat_selected_{watch_id}"] = {c["id"] for c in current}
    await _render_watch_categories(update, context, watch)


async def _render_watch_categories(update, context, watch):
    watch_id = watch["id"]
    back_row = [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")]
    await show_panel(
        update, context,
        f"🗂️ <b>{html.escape(watch['label'])}: категорії</b>\n\n" + "Познач одну або кілька категорій, де продають сам товар (напр. консолі, а не ігри), "
        "і натисни «✅ Готово». Кілька категорій корисні, коли продавці кладуть той самий товар "
        "у різні місця.\n"
        "У дужках — кількість оголошень. ⚠️ — аксесуари й запчастини: зазвичай їх обирати не треба.\n"
        "Кожна додаткова категорія — ще один запит до eBay на кожну перевірку."
        + "\nПісля зміни категорій продажі лишаються, а поточні оголошення бот збере заново."
        + (f"\n\n{CATEGORY_TREE_NOTE}"
           if any(o.get("parent") for o in context.user_data.get(f"cat_options_{watch_id}") or []) else ""),
        reply_markup=_category_keyboard(
            context.user_data.get(f"cat_options_{watch_id}") or [], f"setcat:{watch_id}:",
            context.user_data.get(f"cat_selected_{watch_id}") or set(), extra_rows=[back_row],
        ),
        parse_mode=ParseMode.HTML,
    )


@require_access
async def set_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    _, watch_id_str, choice = query_cb.data.split(":", 2)
    watch_id = int(watch_id_str)
    chat_id = update.effective_chat.id
    watch = get_watch(watch_id, chat_id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return

    options = context.user_data.get(f"cat_options_{watch_id}")
    selected = context.user_data.get(f"cat_selected_{watch_id}")
    if options is None or selected is None:
        await query_cb.answer("Список категорій застарів — відкрий його знову.", show_alert=True)
        return
    old_ids = set(watch_category_ids(watch))
    if choice == "all":
        new_ids = set()
        update_watch_categories(watch_id, chat_id, [])
    elif choice == "done":
        if not selected:
            await query_cb.answer("Познач хоча б одну категорію або обери «Усі категорії».", show_alert=True)
            return
        chosen = await run_with_progress(
            update, context, "🗂️ ⏳ Зберігаю категорії…", collapse_categories, [{"id": o["id"], "name": o["name"]} for o in options if o["id"] in selected])
        new_ids = {c["id"] for c in chosen}
        update_watch_categories(watch_id, chat_id, chosen)
    else:
        try:
            cat_id = options[int(choice)]["id"]
        except (ValueError, IndexError):
            return
        selected.symmetric_difference_update({cat_id})
        await _render_watch_categories(update, context, watch)
        return
    context.user_data.pop(f"cat_options_{watch_id}", None)
    context.user_data.pop(f"cat_selected_{watch_id}", None)
    # Категорії лише ДОДАНО (або «усі категорії» замість обмеження): старі оголошення й
    # продажі лишаються правдивими — зберігаємо історію, ринок просто перерахується з ширшою вибіркою
    removed = old_ids - new_ids
    if removed and new_ids:
        # Прибрана категорія входила в залишену батьківську — по суті нічого не прибрано
        try:
            inside = set()
            for cid in new_ids:
                inside |= await asyncio.to_thread(category_descendants, cid)
            removed -= inside
        except Exception as e:
            log.debug("Не вдалося перевірити вкладеність категорій: %s", e)
    only_added = (not new_ids) or not removed
    if new_ids == old_ids:
        pass
    elif only_added:
        mark_market_stale(watch_id)
    else:
        # Характеристики різних категорій різні — повертаємо автоматичний режим
        update_watch_required_aspect(watch_id, chat_id, None, None)
        # Категорію прибрано/замінено: продажі лишаються, поточні оголошення бот збере заново
        reset_active_listings(watch_id)
        mark_market_stale(watch_id)
    await _show_watch_details(update, context, get_watch(watch_id, chat_id))


EDIT_VALUE = 0  # окрема коротка розмова: введення нового значення


@require_access
async def edit_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«✏️ Редагувати» — що саме змінити в товарі."""
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return ConversationHandler.END
    context.user_data.pop("edit", None)
    min_price = watch.get("min_price") or 0
    rows = [
        [InlineKeyboardButton("🏷 Категорії eBay", callback_data=f"chcat:{watch_id}"),
         InlineKeyboardButton("🗂 Папка", callback_data=f"fld:{watch_id}")],
        [InlineKeyboardButton(
            f"💶 Мінімальна ціна: {f'{min_price:.0f}€' if min_price else 'авто'}",
            callback_data=f"edmin:{watch_id}")],
        [InlineKeyboardButton("🧾 Обов'язкові характеристики", callback_data=f"reqasp:{watch_id}")],
        [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")],
    ]
    await show_panel(
        update, context,
        title("✏️", f"Редагування · {html.escape(watch['label'])}", "що хочеш змінити?") + "\n\n"
        "🏷 <b>Категорії eBay</b> — де шукати оголошення (напр. консолі, а не ігри).\n"
        "🗂 <b>Папка</b> — у якій категорії товар показується в «🗂 Категорії товарів».\n"
        "💶 <b>Мінімальна ціна</b> — дешевші оголошення не враховуються (відсіює аксесуари).\n"
        "🧾 <b>Характеристики</b> — що обов'язково має бути в оголошенні.",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


@require_access
async def edit_value_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """edmin:<id> — мінімальна ціна."""
    query_cb = update.callback_query
    _, watch_id_str = query_cb.data.split(":")
    watch_id = int(watch_id_str)
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return ConversationHandler.END
    field = "min"
    context.user_data["edit"] = {"field": field, "watch_id": watch_id}
    back = [InlineKeyboardButton("◀️ Назад", callback_data=f"editw:{watch_id}")]

    stats = get_market_stats(watch_id)
    medians = sorted(s["median_price"] for s in stats)
    presets = []
    if medians:
        base = medians[0]
        presets = sorted({max(5, round(base * pct / 100 / 5) * 5) for pct in (30, 40, 50)})
    rows = []
    if presets:
        rows.append([InlineKeyboardButton(f"{p:.0f}€", callback_data=f"edval:{p}") for p in presets])
    rows.append([InlineKeyboardButton("🤖 Автоматично", callback_data="edval:0")])
    rows.append(back)
    current = watch.get("min_price") or 0
    auto_min = get_auto_min_price(watch_id)
    auto_txt = f"автоматично (зараз від {auto_min:.0f}€)" if auto_min else "автоматично (після першого аналізу цін)"
    text = (
        f"💶 <b>{html.escape(watch['label'])}: мінімальна ціна</b>\nЗараз: {f'{current:.0f}€' if current else auto_txt}\n\n"
        "Оголошення дешевші за цю ціну eBay боту не віддає — так відсіюються аксесуари й запчастини, "
        "і справжні оголошення не губляться серед них у великих категоріях."
        + ("\nКнопки — 30, 40 і 50% від найнижчої типової ціни." if presets else "")
        + f"\n\n🤖 Автоматично — {MIN_PRICE_SUGGESTION_PCT}% від найнижчої типової ціни, бот рахує сам."
        + "\n\nОбери кнопкою або надішли число в євро (0 — автоматично)."
    )
    await show_panel(update, context, text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
    return EDIT_VALUE


async def _apply_edit(update, context, raw_value):
    edit = context.user_data.get("edit") or {}
    watch_id = edit.get("watch_id")
    chat_id = update.effective_chat.id
    watch = get_watch(watch_id, chat_id) if watch_id else None
    if watch is None:
        context.user_data.pop("edit", None)
        await show_main_menu(update, context)
        return ConversationHandler.END
    back = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"editw:{watch_id}")]])
    try:
        value = float(str(raw_value).replace("%", "").replace("€", "").replace(",", ".").strip())
    except ValueError:
        await show_panel(update, context, "Це не схоже на число. Спробуй ще раз.", reply_markup=back)
        return EDIT_VALUE

    if value < 0:
        await show_panel(update, context, "Ціна не може бути від'ємною. Спробуй ще раз.", reply_markup=back)
        return EDIT_VALUE
    update_watch_min_price(watch_id, chat_id, value)
    # Історію не стираємо — прибираємо лише оголошення, дешевші за нову мінімальну ціну
    await asyncio.to_thread(apply_filters_to_history, get_watch(watch_id, chat_id))

    context.user_data.pop("edit", None)
    await _show_watch_details(update, context, get_watch(watch_id, chat_id))
    return ConversationHandler.END


async def edit_value_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ack_callback(update)
    return await _apply_edit(update, context, update.callback_query.data.split(":", 1)[1])


async def edit_value_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _apply_edit(update, context, update.message.text)


async def edit_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Будь-яка інша кнопка під час введення значення — вийти без змін і
    виконати натиснуту дію."""
    context.user_data.pop("edit", None)
    data = update.callback_query.data
    if data.startswith("editw:"):
        return await edit_menu_callback(update, context)
    if data.startswith("watch_details:"):
        await watch_details_callback(update, context)
    elif data == "menu:list":
        await cmd_list(update, context)
    elif data == "menu:refresh_usage":
        await refresh_usage_callback(update, context)
    elif data == "menu:refresh_prices":
        await refresh_all_callback(update, context)
    elif data == "menu:discover":
        await discover_callback(update, context)
    else:
        await show_main_menu(update, context)
    return ConversationHandler.END


async def edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("edit", None)
    await show_main_menu(update, context)
    return ConversationHandler.END


# Імпорти з інших екранів — унизу, щоб модулі могли посилатися один на одного
from ui.screen_common import (  # noqa: E402
    CATEGORY_TREE_NOTE,
    ASPECT_CHOICE_TEXT,
    _aspect_keyboard,
    _category_keyboard,
)
from ui.screen_discover import (  # noqa: E402
    discover_callback,
)
from ui.screen_categories import (  # noqa: E402
    cmd_list,
)
from ui.screen_watch import (  # noqa: E402
    _show_watch_details,
    refresh_all_callback,
    watch_details_callback,
)
