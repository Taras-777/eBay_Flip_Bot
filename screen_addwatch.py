"""
Діалог додавання товару: назва → (спільні дані ринку) → категорії → характеристики → мінімальна ціна.
"""

import asyncio
import html
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from settings import DEFAULT_CONDITION_IDS, MIN_PRICE_SUGGESTION_PCT, log
from textparse import category_label, plural
from db import (
    add_watch,
    encode_required_aspects,
    find_duplicate_watch,
    get_required_aspects,
    watch_obs_summary,
    get_watch,
    list_watches,
)
from ebay_api import aspect_options_for_categories, get_category_options
from market import suggest_min_price
from panel import (
    refresh_usage_callback,
    _ack_callback,
    build_main_menu,
    cancel_keyboard,
    main_menu_text,
    show_main_menu,
    show_panel,
)
from access import cmd_pending, cmd_users, require_access
from shared_market import attach_shared_history, find_shared_watch, shared_categories


# ============================================================
# ДІАЛОГ ДОДАВАННЯ ВІДСТЕЖЕННЯ (/addwatch)
# Кроки: назва → категорії eBay → обов'язкові характеристики → мінімальна ціна
# ============================================================

(
    ASK_QUERY,
    ASK_CATEGORY,
    ASK_ASPECT,
    ASK_MIN_PRICE_CHOICE,
    ASK_CUSTOM_MIN_PRICE,
) = range(5)


NEW_WATCH_KEYS = (
    "new_watch_query", "new_watch_category_id", "new_watch_category_name",
    "new_watch_categories", "new_watch_category_selected",
    "new_watch_category_options", "new_watch_min_price", "suggested_min_price",
    "new_watch_aspect_options", "new_watch_aspect_selected", "new_watch_required_aspect", "new_watch_require_spec",
    "new_watch_shared_from",
)


def _clear_new_watch(context):
    for key in NEW_WATCH_KEYS:
        context.user_data.pop(key, None)


@require_access
async def addwatch_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_new_watch(context)
    await show_panel(
        update, context,
        "📦 <b>Додавання товару</b>\n\n"
        "Введи назву/модель товару, який хочеш відстежувати на eBay.\n"
        "Приклад: iPhone 13 128GB, PlayStation 5 Pro, ThinkPad X1 Carbon Gen 10",
        reply_markup=cancel_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ASK_QUERY


async def addwatch_got_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    if not query:
        await show_panel(update, context, "Назва не може бути порожньою. Спробуй ще раз.", reply_markup=cancel_keyboard())
        return ASK_QUERY

    chat_id = update.effective_chat.id
    duplicate = find_duplicate_watch(chat_id, query)
    if duplicate:
        await show_panel(
            update, context,
            f"⚠️ У тебе вже є товар «{html.escape(duplicate['query'])}» "
            f"з такою ж назвою.\n\n"
            f"Введи іншу назву — наприклад, додай конкретний обсяг пам'яті чи стан, "
            f"щоб відрізнити від наявного запису.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_QUERY

    context.user_data["new_watch_query"] = query

    if not _ebay_configured():
        return await _finalize_watch(update, context)

    # Той самий товар уже відстежує інший користувач — пропонуємо спільні дані ринку
    shared = find_shared_watch(query, exclude_chat_id=chat_id)
    if shared:
        obs = watch_obs_summary(shared["id"])
        cats = ", ".join(category_label(c["name"]) for c in shared_categories(shared)) or "усі категорії"
        context.user_data["new_watch_shared_from"] = shared["id"]
        await show_panel(
            update, context,
            f"💡 <b>«{html.escape(query)}» уже аналізується ботом</b>\n\n"
            f"📋 відстежується оголошень: {obs['active']}\n"
            f"🛒 помічено продажів: {obs['sold']}\n"
            f"🗂️ категорії: {html.escape(cats)}\n\n"
            "Підключитися до готових даних? Ціни й продажі з'являться одразу, а eBay не "
            "скануватиметься двічі. Стан, мінімальну ціну й характеристики налаштуєш під себе, "
            "а сховані оголошення й пропозиції в тебе будуть свої.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Підключитися", callback_data="shr:yes")],
                [InlineKeyboardButton("⚙️ Ні, оберу категорії сам", callback_data="shr:no")],
                [InlineKeyboardButton("❌ Скасувати", callback_data="menu:home")],
            ]),
            parse_mode=ParseMode.HTML,
        )
        return ASK_CATEGORY
    return await _ask_categories(update, context)


async def addwatch_shared_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """shr:yes — ті самі категорії, що в уже відстежуваного товару; shr:no — звичайний вибір."""
    await _ack_callback(update)
    shared_id = context.user_data.pop("new_watch_shared_from", None)
    shared = next((w for w in list_watches(active_only=True) if w["id"] == shared_id), None)
    if update.callback_query.data == "shr:yes" and shared:
        context.user_data["new_watch_categories"] = shared_categories(shared)
        if not context.user_data["new_watch_categories"]:
            return await _propose_min_price(update, context)
        return await _propose_aspects(update, context)
    return await _ask_categories(update, context)


async def _ask_categories(update, context):
    query = context.user_data["new_watch_query"]
    await show_panel(
        update, context,
        f"🔎 «{html.escape(query)}»\n\nШукаю, в яких категоріях eBay є такі товари…",
        reply_markup=cancel_keyboard(),
    )
    try:
        options = await asyncio.to_thread(get_category_options, query)
    except Exception as e:
        log.warning("Не вдалося отримати категорії для «%s»: %s", query, e)
        options = []

    if not options:
        # Категорій не знайшлось — продовжуємо без обмеження категорією
        return await _propose_min_price(update, context)

    context.user_data["new_watch_category_options"] = options
    context.user_data["new_watch_category_selected"] = set()
    await _render_new_watch_categories(update, context)
    return ASK_CATEGORY


async def _render_new_watch_categories(update, context):
    query = context.user_data["new_watch_query"]
    options = context.user_data.get("new_watch_category_options") or []
    selected = context.user_data.get("new_watch_category_selected") or set()
    await show_panel(
        update, context,
        f"🗂️ <b>«{html.escape(query)}»: обери категорії</b>\n\n" + "Познач одну або кілька категорій, де продають сам товар (напр. консолі, а не ігри), "
        "і натисни «✅ Готово». Кілька категорій корисні, коли продавці кладуть той самий товар "
        "у різні місця.\n"
        "У дужках — кількість оголошень. ⚠️ — аксесуари й запчастини: зазвичай їх обирати не треба.\n"
        "Кожна додаткова категорія — ще один запит до eBay на кожну перевірку.",
        reply_markup=_category_keyboard(
            options, "cat:", selected,
            extra_rows=[[InlineKeyboardButton("❌ Скасувати", callback_data="menu:home")]],
        ),
        parse_mode=ParseMode.HTML,
    )


async def addwatch_category_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    await _ack_callback(update)
    choice = query_cb.data.split(":", 1)[1]
    options = context.user_data.get("new_watch_category_options") or []
    selected = context.user_data.setdefault("new_watch_category_selected", set())

    if choice == "all":
        context.user_data["new_watch_categories"] = []
        return await _propose_min_price(update, context)
    if choice == "done":
        if not selected:
            await query_cb.answer("Познач хоча б одну категорію або обери «Усі категорії».", show_alert=True)
            return ASK_CATEGORY
        context.user_data["new_watch_categories"] = [
            {"id": o["id"], "name": o["name"]} for o in options if o["id"] in selected
        ]
        return await _propose_aspects(update, context)
    try:
        cat_id = options[int(choice)]["id"]
    except (ValueError, IndexError):
        return ASK_CATEGORY
    selected.symmetric_difference_update({cat_id})
    await _render_new_watch_categories(update, context)
    return ASK_CATEGORY


async def _propose_aspects(update, context):
    """Крок після категорій: які характеристики мають бути заповнені в оголошенні."""
    query = context.user_data["new_watch_query"]
    category_ids = [c["id"] for c in context.user_data.get("new_watch_categories") or []]
    await show_panel(
        update, context,
        f"🔎 «{html.escape(query)}»\n\nДізнаюсь характеристики категорії в eBay…",
        reply_markup=cancel_keyboard(),
    )
    try:
        options = await asyncio.to_thread(aspect_options_for_categories, category_ids, query)
    except Exception as e:
        log.warning("Не вдалося отримати характеристики для «%s»: %s", query, e)
        options = []
    if not options:
        return await _propose_min_price(update, context)  # нічого запропонувати — далі
    context.user_data["new_watch_aspect_options"] = options
    context.user_data["new_watch_aspect_selected"] = set()
    await _render_new_watch_aspects(update, context)
    return ASK_ASPECT


async def _render_new_watch_aspects(update, context):
    query = context.user_data["new_watch_query"]
    options = context.user_data.get("new_watch_aspect_options") or []
    selected = context.user_data.get("new_watch_aspect_selected") or set()
    rows = []
    for idx, opt in enumerate(options):
        mark = "☑️" if opt["name"] in selected else "⬜"
        badge = " ❗" if opt.get("required") else ""
        rows.append([InlineKeyboardButton(f"{mark} {opt['name'][:36]}{badge}", callback_data=f"nasp:{idx}")])
    rows.append([InlineKeyboardButton(f"💾 Зберегти вибір ({len(selected)})", callback_data="nasp:save")])
    rows.append([
        InlineKeyboardButton("🤖 Авто", callback_data="nasp:auto"),
        InlineKeyboardButton("🚫 Без вимоги", callback_data="nasp:none"),
    ])
    rows.append([InlineKeyboardButton("❌ Скасувати", callback_data="menu:home")])
    await show_panel(
        update, context,
        f"🧾 <b>«{html.escape(query)}»: обов'язкові характеристики</b>\n\n{ASPECT_CHOICE_TEXT}",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


async def addwatch_aspect_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """nasp:<індекс> — позначити/зняти, nasp:save / nasp:auto / nasp:none — далі."""
    await _ack_callback(update)
    choice = update.callback_query.data.split(":", 1)[1]
    options = context.user_data.get("new_watch_aspect_options") or []
    selected = context.user_data.setdefault("new_watch_aspect_selected", set())

    if choice in ("save", "auto", "none"):
        ordered = [o["name"] for o in options if o["name"] in selected] if choice == "save" else []
        context.user_data["new_watch_required_aspect"] = encode_required_aspects(ordered)
        # Нічого не позначено або «Авто» → автоматичний режим; «Без вимоги» → вимкнено
        context.user_data["new_watch_require_spec"] = 0 if (ordered or choice == "none") else None
        return await _propose_min_price(update, context)
    try:
        name = options[int(choice)]["name"]
    except (ValueError, IndexError):
        return ASK_ASPECT
    selected.symmetric_difference_update({name})
    await _render_new_watch_aspects(update, context)
    return ASK_ASPECT


async def _propose_min_price(update, context):
    query = context.user_data["new_watch_query"]
    categories = context.user_data.get("new_watch_categories") or []
    category_ids = [c["id"] for c in categories]
    category_name = ", ".join(category_label(c["name"]) for c in categories)

    await show_panel(
        update, context,
        f"🔎 «{html.escape(query)}»\n\nАналізую ціни, щоб запропонувати мінімальну ціну…",
        reply_markup=cancel_keyboard(),
    )
    suggestion = await asyncio.to_thread(suggest_min_price, query, category_ids)
    if suggestion is None:
        # Замало даних для пропозиції — без мінімальної ціни
        context.user_data["new_watch_min_price"] = 0
        return await _finalize_watch(update, context)

    min_price, median_price, sample_size = suggestion
    context.user_data["suggested_min_price"] = min_price
    cat_line = f"Категорія: {html.escape(category_name)}\n" if category_name else ""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ Від {min_price:.0f}€", callback_data="minp:use"),
            InlineKeyboardButton("✏️ Своя ціна", callback_data="minp:custom"),
        ],
        [InlineKeyboardButton("🤖 Автоматично", callback_data="minp:none")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="menu:home")],
    ])
    await show_panel(
        update, context,
        f"💶 <b>«{html.escape(query)}»: мінімальна ціна</b>\n\n"
        f"{cat_line}"
        f"Типова ціна зараз: ~{median_price:.0f}€ (за {plural(sample_size, 'оголошенням', 'оголошеннями', 'оголошеннями')}).\n\n"
        f"Пропоную не враховувати оголошення дешевші за <b>{min_price:.0f}€</b> "
        f"(~{MIN_PRICE_SUGGESTION_PCT}% від типової ціни) — так відсіюються ігри, "
        f"аксесуари й запчастини, які майже завжди значно дешевші за сам товар.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )
    return ASK_MIN_PRICE_CHOICE


async def addwatch_min_price_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    await _ack_callback(update)
    choice = query_cb.data.split(":", 1)[1]

    if choice == "custom":
        await show_panel(
            update, context,
            "✏️ Введи мінімальну ціну в євро (напр. 120), або 0 — автоматично.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_CUSTOM_MIN_PRICE
    if choice == "use":
        context.user_data["new_watch_min_price"] = context.user_data.get("suggested_min_price", 0)
    else:
        context.user_data["new_watch_min_price"] = 0
    return await _finalize_watch(update, context)


async def addwatch_custom_min_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace("€", "").replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        await show_panel(update, context, "Це не схоже на число. Введи, наприклад: 120", reply_markup=cancel_keyboard())
        return ASK_CUSTOM_MIN_PRICE
    if value < 0:
        await show_panel(update, context, "Ціна не може бути від'ємною. Спробуй ще раз.", reply_markup=cancel_keyboard())
        return ASK_CUSTOM_MIN_PRICE
    context.user_data["new_watch_min_price"] = value
    return await _finalize_watch(update, context)


async def _finalize_watch(update, context):
    query = context.user_data["new_watch_query"]
    categories = context.user_data.get("new_watch_categories") or []
    category_name = ", ".join(category_label(c["name"]) for c in categories)
    min_price = context.user_data.get("new_watch_min_price") or 0
    required_aspect = context.user_data.get("new_watch_required_aspect")
    require_spec = context.user_data.get("new_watch_require_spec")
    _clear_new_watch(context)
    chat_id = update.effective_chat.id

    wid = add_watch(
        chat_id=chat_id,
        label=query,
        query=query,
        exclude="broken defekt teile parts kaputt",
        condition_ids=DEFAULT_CONDITION_IDS,
        categories=categories,
        min_price=min_price,
        require_spec=require_spec,
        required_aspect=required_aspect,
    )

    extras = []
    try:
        copied = await asyncio.to_thread(attach_shared_history, get_watch(wid, chat_id))
    except Exception as e:
        log.warning("Не вдалося підключити спільні дані ринку для watch #%s: %s", wid, e)
        copied = 0
    if copied:
        extras.append(f"📊 Підключено готові дані ринку: {plural(copied, 'оголошення', 'оголошення', 'оголошень')} "
                      f"(продажів: {watch_obs_summary(wid)['sold']}) — ціни порахуються за кілька хвилин")
    if category_name:
        extras.append(f"🗂️ Категорії: {html.escape(category_name)}")
    if min_price:
        extras.append(f"💶 Мінімальна ціна: {min_price:.0f}€")
    aspects = get_required_aspects({"required_aspect": required_aspect})
    if aspects:
        extras.append(f"🧾 Обов'язкові характеристики: {html.escape(', '.join(aspects))}")
    extras_txt = ("\n" + "\n".join(extras)) if extras else ""

    user_id = update.effective_user.id
    text = (
        f"✅ Товар «{html.escape(query)}» додано."
        f"{extras_txt}\n\n{main_menu_text(update.effective_user.id)}"
    )
    await show_panel(update, context, text, reply_markup=build_main_menu(user_id), parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def addwatch_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_new_watch(context)
    user_id = update.effective_user.id
    await show_panel(
        update, context,
        f"Скасовано.\n\n{main_menu_text(update.effective_user.id)}",
        reply_markup=build_main_menu(user_id),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def addwatch_menu_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка меню посеред /addwatch — скасовує без збереження і
    відкриває натиснутий розділ у тій же панелі."""
    action = update.callback_query.data.split(":", 1)[1]
    _clear_new_watch(context)

    if action == "addwatch":
        return await addwatch_start(update, context)
    if action == "home":
        await show_main_menu(update, context)
    elif action == "list":
        await cmd_list(update, context)
    elif action == "stats":
        await cmd_stats(update, context)
    elif action == "pending":
        await cmd_pending(update, context)
    elif action == "users":
        await cmd_users(update, context)
    elif action == "refresh_usage":
        await refresh_usage_callback(update, context)
    elif action == "refresh_prices":
        await refresh_all_callback(update, context)
    elif action == "discover":
        await discover_callback(update, context)
    else:
        await show_main_menu(update, context)
    return ConversationHandler.END


# Імпорти з інших екранів — унизу, щоб модулі могли посилатися один на одного
from screen_common import (  # noqa: E402
    ASPECT_CHOICE_TEXT,
    _category_keyboard,
    _ebay_configured,
)
from screen_watch import (  # noqa: E402
    cmd_list,
    cmd_stats,
    refresh_all_callback,
)
from screen_discover import (  # noqa: E402
    discover_callback,
)
