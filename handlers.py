"""
Екрани й діалоги бота: додавання, перегляд, редагування й видалення товарів,
команди користувача.
"""

import asyncio
import config
import html
import statistics
import time
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from settings import (
    DEFAULT_CONDITION_IDS,
    DEFAULT_DISCOUNT_THRESHOLD_PCT,
    MAX_SPEC_LOOKUPS_PER_DEAL_SCAN,
    MIN_PRICE_SUGGESTION_PCT,
    MIN_PROFIT_EUR,
    MIN_SAMPLE_SIZE,
    MIN_PROFIT_CHOICES,
    MIN_SOLD_SAMPLE,
    SALES_WINDOW_DAYS,
    SEARCH_RESERVE,
    SOLD_LOOKBACK_DAYS,
    LOCAL_TZ,
    is_owner,
    log,
)
from textparse import CONDITION_LABELS, _group_label, _search_tokens, category_label, is_accessory_category, plural
from learning import hide_item, learned_words_note, reject_and_learn, unlearn_word
from checker import CheckError, check_listing
from discovery import run_discovery, top_recommendations
from db import (
    delete_listing_obs_by_ids,
    add_watch,
    encode_required_aspects,
    find_duplicate_watch,
    get_current_listings,
    get_deal,
    get_auto_min_price,
    get_deal_owner_chat_id,
    get_inbox_deals,
    mark_deals_seen,
    clear_inbox_deals,
    get_learned_words,
    get_deal_stats,
    get_market_stats,
    get_min_profit,
    get_price_history,
    set_min_profit,
    get_required_aspects,
    get_sold_listings,
    watch_obs_summary,
    get_user_row,
    get_watch,
    get_watch_categories,
    list_watches,
    remove_watch,
    reset_watch_market,
    set_deal_status,
    update_watch_categories,
    update_watch_min_price,
    update_watch_required_aspect,
    upsert_user_request,
    watch_category_ids,
)
from ebay_api import (
    _watch_search_kwargs,
    aspect_options_for_categories,
    browse_budget_left,
    fetch_browse_rate_limit,
    get_category_options,
    search_in_categories,
)
from market import (
    _annotate_items,
    _apply_item_filters,
    _recalculate_watch_medians,
    estimate_resale_profit,
    refresh_sale_prices,
    filter_outliers,
    max_buy_price,
    minimum_sample_size_for_query,
    suggest_min_price,
    watch_requires_spec,
)
from panel import (
    refresh_usage_callback,
    _ack_callback,
    back_to_menu_keyboard,
    build_main_menu,
    cancel_keyboard,
    main_menu_text,
    show_main_menu,
    show_panel,
)
from access import _notify_owner_new_request, cmd_pending, cmd_users, require_access
from notifications import _notify_median_error, _notify_median_problem
from ebay_user import is_connected
from shared_market import attach_shared_history, find_shared_watch, shared_categories
from sales import sales_note, speed_text, summarize, weekly_change


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


def _ebay_configured():
    return bool(config.EBAY_CLIENT_ID) and "PUT_YOUR" not in config.EBAY_CLIENT_ID


def _category_keyboard(options, prefix, selected=(), extra_rows=None):
    """Перемикачі категорій: f'{prefix}{index}' — позначити/зняти,
    f'{prefix}done' — зберегти вибір, f'{prefix}all' — без обмеження категорією.
    Категорії аксесуарів/запчастин позначені ⚠️ і показані останніми."""
    rows = []
    order = sorted(range(len(options)), key=lambda i: is_accessory_category(options[i]["name"]))
    for idx in order:
        opt = options[idx]
        shown = category_label(opt["name"])
        label = shown if len(shown) <= 34 else shown[:31] + "…"
        mark = "☑️" if opt["id"] in selected else "⬜"
        warn = " ⚠️" if is_accessory_category(opt["name"]) else ""
        rows.append([InlineKeyboardButton(f"{mark} {label} ({opt['count']}){warn}", callback_data=f"{prefix}{idx}")])
    rows.append([InlineKeyboardButton(f"✅ Готово ({len(selected)} обрано)", callback_data=f"{prefix}done")])
    rows.append([InlineKeyboardButton("🌐 Усі категорії (без обмеження)", callback_data=f"{prefix}all")])
    for row in extra_rows or []:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _aspect_keyboard(watch_id, options, selected, extra_rows=None):
    """Перемикачі характеристик ☑️/⬜ + збереження, авто, без вимоги."""
    rows = []
    for idx, opt in enumerate(options):
        mark = "☑️" if opt["name"] in selected else "⬜"
        badge = " ❗" if opt.get("required") else ""
        rows.append([InlineKeyboardButton(f"{mark} {opt['name'][:36]}{badge}", callback_data=f"tglasp:{watch_id}:{idx}")])
    rows.append([InlineKeyboardButton(f"💾 Зберегти вибір ({len(selected)})", callback_data=f"saveasp:{watch_id}")])
    rows.append([
        InlineKeyboardButton("🤖 Авто", callback_data=f"setasp:{watch_id}:auto"),
        InlineKeyboardButton("🚫 Без вимоги", callback_data=f"setasp:{watch_id}:none"),
    ])
    for row in extra_rows or []:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


ASPECT_CHOICE_TEXT = (
    "Познач одну або кілька характеристик і натисни «💾 Зберегти». Оголошення враховуватиметься, "
    "лише якщо в ньому заповнені ВСІ позначені характеристики — інакше вважається аксесуаром.\n"
    "❗ — обов'язкова в цій категорії (продавець не може її пропустити).\n"
    "🤖 Авто — для телефонів, ноутбуків і консолей бот сам вимагає обсяг пам'яті; 🚫 — без вимоги.\n\n"
    "⚠️ Якщо характеристик зазвичай немає в назві, бот перевірятиме характеристики кожного "
    "оголошення — це додаткові запити до eBay (з кешем і лімітом). "
    "Оголошення з «Kompatible Marke/Modell» відкидаються завжди."
)


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
        discount_threshold_pct=DEFAULT_DISCOUNT_THRESHOLD_PCT,  # колонка лишилась у БД, не використовується
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


# ============================================================
# ІНШІ TELEGRAM-КОМАНДИ
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_owner(user.id):
        row = get_user_row(user.id)
        status = row["status"] if row else None
        if status != "approved":
            if status == "pending":
                await update.message.reply_text("⏳ Твій запит на доступ ще розглядається власником бота.")
            else:
                upsert_user_request(user.id, update.effective_chat.id, user.username, user.first_name)
                await _notify_owner_new_request(context.bot, user)
                await update.message.reply_text(
                    "🔒 Доступ до цього бота обмежений.\n"
                    "Твій запит надіслано власнику — очікуй підтвердження."
                )
            return

    context.user_data.pop("panel_message_id", None)
    await show_main_menu(update, context)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показати головне меню знову (напр. якщо панель загубилась вище в чаті)."""
    context.user_data.pop("panel_message_id", None)
    await show_main_menu(update, context)


@require_access
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    watches = list_watches(chat_id=chat_id)
    if not watches:
        await show_panel(
            update, context, "У тебе ще немає товарів. Додай перший:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Додати товар", callback_data="menu:addwatch")],
                [InlineKeyboardButton("◀️ Меню", callback_data="menu:home")],
            ]),
        )
        return
    lines = [f"📦 <b>Мої товари ({len(watches)})</b>\n", "Обери товар, щоб відкрити його:"]
    rows = []
    for w in watches:
        no_cat = " ⚠️" if not watch_category_ids(w) else ""
        label = w["label"] if len(w["label"]) <= 40 else w["label"][:39] + "…"
        rows.append([InlineKeyboardButton(f"📦 {label}{no_cat}", callback_data=f"watch_details:{w['id']}")])
    if any(not watch_category_ids(w) for w in watches):
        lines.append("\n⚠️ — не обрано категорію eBay (відкрий товар → ✏️ Редагувати)")
    rows.extend(
        [
            [InlineKeyboardButton("➕ Додати товар", callback_data="menu:addwatch")],
            [InlineKeyboardButton("◀️ Меню", callback_data="menu:home")],
        ]
    )
    await show_panel(
        update,
        context,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


def _watch_details_text(watch):
    stats = get_market_stats(watch["id"])
    min_profit = get_min_profit(watch["chat_id"])
    category_txt = html.escape(
        ", ".join(category_label(c["name"], with_original=True) for c in get_watch_categories(watch))
    ) or "усі (не обрано)"
    min_price = watch.get("min_price") or 0
    auto_min = None if min_price else get_auto_min_price(watch["id"])
    if min_price:
        min_price_txt = f"{min_price:.0f}€"
    elif auto_min:
        min_price_txt = f"автоматично від {auto_min:.0f}€ (щоб відсіяти аксесуари)"
    else:
        min_price_txt = "без обмеження"
    lines = [
        f"📌 <b>{html.escape(watch['label'])}</b>",
        f"🎯 Вигідно, якщо чистий прибуток ≥ <b>{min_profit:.0f}€</b> (з урахуванням комісії eBay і доставки)",
        f"🗂️ Категорії: {category_txt}",
        f"💶 Мінімальна ціна: {min_price_txt}",
        (f"🧾 Обов'язкові характеристики: {html.escape(', '.join(get_required_aspects(watch)))}"
         if get_required_aspects(watch) else
         "💾 Лише оголошення з відомою пам'яттю: "
         + ("так" if watch_requires_spec(watch) else "ні")
         + (" (авто)" if watch.get("require_spec") is None else "")),
    ]
    obs = watch_obs_summary(watch["id"])
    if obs["active"] or obs["sold"] or obs["pending"]:
        sold_line = f"🛒 Продано за {SOLD_LOOKBACK_DAYS} днів: <b>{obs['sold']}</b>"
        if obs["confirmed"]:
            sold_line += f" (✅ підтверджено eBay: {obs['confirmed']})"
        lines += ["", "👁 <b>Спостереження:</b>",
                  f"📋 Відстежую оголошень зараз: <b>{obs['active']}</b>", sold_line]
        if obs["pending"]:
            lines.append(f"⏳ У черзі на перевірку «продано?»: <b>{obs['pending']}</b>")
        if obs["withdrawn"]:
            lines.append(f"🚫 Зникли без продажу (не враховуються): {obs['withdrawn']}")
    learned = sorted(w for w, st in get_learned_words(watch["id"]).items() if st == "excluded")
    if learned:
        lines.append(f"🧠 Відсіюю за вивченими словами: {html.escape(', '.join(learned))}")
    if not stats:
        lines.append("\n💰 <b>Ціни ще не пораховані</b> — потрібно щонайменше "
                     f"{plural(MIN_SAMPLE_SIZE, 'оголошення', 'оголошення', 'оголошень')} одного стану.")
        return "\n".join(lines)

    lines.append("\n💰 <b>Купівля і продаж:</b>")
    history = get_price_history(watch["id"])
    for s in sorted(stats, key=lambda s: (s["cond_group"], s["spec_group"] != "*", s["spec_group"])):
        sale_price = s["sale_price"] or s["median_price"]
        buy_limit = max_buy_price(sale_price, min_profit)
        trend = ""
        weekly = weekly_change(history.get((s["cond_group"], s["spec_group"])))
        if weekly and abs(weekly[0]) >= 1:
            arrow = "📉" if weekly[0] < 0 else "📈"
            trend = f" {arrow} {weekly[0]:+.0f}% за тиждень"
        elif s["prev_median_price"]:
            if s["median_price"] < s["prev_median_price"] * 0.98:
                trend = " ⬇️"
            elif s["median_price"] > s["prev_median_price"] * 1.02:
                trend = " ⬆️"
        lines.append(
            f"\n• <b>{html.escape(_group_label(s['cond_group'], s['spec_group']))}</b> "
            f"(оголошень: {s['sample_size']})\n"
            f"  🛒 Купувати до: <b>{buy_limit:.0f}€</b>\n"
            f"  💶 Продати за: ~<b>{sale_price:.0f}€</b> ({html.escape(s['sale_source'] or 'оцінка')})\n"
            f"  📊 Типова ціна оголошень: {s['median_price']:.0f}€{trend}"
        )
    lines.append(
        "\n<i>«Продати за» — оцінка за найдешевшою чвертю поточних оголошень. Коли бот побачить "
        f"щонайменше {MIN_SOLD_SAMPLE} проданих, рахуватиме за ними. "
        f"«Купувати до» лишає ≥{min_profit:.0f}€ після комісії eBay і доставки.</i>"
    )
    return "\n".join(lines)


async def _show_watch_details(update, context, watch):
    watch_id = watch["id"]
    rows = [
        [InlineKeyboardButton("🔎 Переглянути оголошення", callback_data=f"view_listings:{watch_id}")],
        [InlineKeyboardButton("🔍 Перевірити оголошення", callback_data=f"chkl:{watch_id}")],
        [
            InlineKeyboardButton("🧩 Усі конфігурації", callback_data=f"configs:{watch_id}"),
            InlineKeyboardButton("📈 Продажі", callback_data=f"sales:{watch_id}"),
        ],
        [InlineKeyboardButton("🔄 Оновити ціни", callback_data=f"recalc_median:{watch_id}")],
        [
            InlineKeyboardButton("✏️ Редагувати", callback_data=f"editw:{watch_id}"),
            InlineKeyboardButton("🗑️ Видалити", callback_data=f"delwatch_ask:{watch_id}"),
        ],
        [InlineKeyboardButton("◀️ До моїх товарів", callback_data="menu:list")],
    ]
    await show_panel(
        update,
        context,
        _watch_details_text(watch),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


@require_access
async def watch_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    await _show_watch_details(update, context, watch)


@require_access
async def all_configs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Усі конфігурації з останнього ринкового сканування — зокрема ті, де
    оголошень замало для окремої оцінки (вони оцінюються через групу
    «усі конфігурації» відповідного стану)."""
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return

    back_rows = [[InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")]]
    listings = get_current_listings(watch_id)
    if not listings:
        await show_panel(
            update, context,
            f"🧩 <b>{html.escape(watch['label'])}: конфігурації</b>\n\n"
            "Ще немає даних з ринкового сканування. На екрані товару натисни "
            "«🔄 Оновити ціни» й відкрий цей список знову.",
            reply_markup=InlineKeyboardMarkup(back_rows),
            parse_mode=ParseMode.HTML,
        )
        return

    groups = {}
    for row in listings:
        groups.setdefault((row["cond_group"], row["spec_group"]), []).append(row["price"])

    lines = [
        f"🧩 <b>{html.escape(watch['label'])}: усі конфігурації</b>",
        f"Оголошень в останньому скануванні: <b>{len(listings)}</b>",
    ]
    current_cond = None
    for (cond, spec), prices in sorted(groups.items(), key=lambda kv: (kv[0][0], statistics.median(kv[1]))):
        if cond != current_cond:
            current_cond = cond
            lines.append(f"\n<b>{html.escape(CONDITION_LABELS.get(cond, cond).capitalize())}</b>")
        spec_txt = "конфігурація не вказана" if spec == "unspecified" else spec
        enough = len(prices) >= MIN_SAMPLE_SIZE
        mark = "" if enough else " ⚠️"
        lines.append(
            f"• <b>{html.escape(spec_txt)}</b>{mark} — {len(prices)} огол., "
            f"від {min(prices):.0f}€, типова {statistics.median(prices):.0f}€"
        )
    lines.append(
        f"\n<i>⚠️ — менше {MIN_SAMPLE_SIZE} оголошень: окремої оцінки «купити/продати» немає, "
        "оголошення цієї конфігурації порівнюються із групою «усі конфігурації» свого стану.</i>\n"
        "Натисни групу нижче, щоб побачити її найдешевші оголошення."
    )

    # Кнопки перегляду оголошень: спершу "усі" для кожного стану, потім конфігурації
    choices = []
    conds = sorted({cond for cond, _ in groups})
    for cond in conds:
        count = sum(len(p) for (c, _), p in groups.items() if c == cond)
        choices.append(((cond, "*"), f"🔎 {CONDITION_LABELS.get(cond, cond).capitalize()} — усі ({count})"))
    for (cond, spec), prices in sorted(groups.items(), key=lambda kv: (kv[0][0], statistics.median(kv[1]))):
        spec_txt = "без конфігурації" if spec == "unspecified" else spec
        choices.append(((cond, spec), f"🔎 {CONDITION_LABELS.get(cond, cond).capitalize()} · {spec_txt} ({len(prices)})"))
    choices = choices[:20]
    context.user_data[f"cfg_groups_{watch_id}"] = [key for key, _ in choices]
    rows = [[InlineKeyboardButton(label[:60], callback_data=f"cfgl:{watch_id}:{i}")]
            for i, (_, label) in enumerate(choices)]
    await show_panel(
        update, context, "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows + back_rows),
        parse_mode=ParseMode.HTML,
    )


def _ago(ts):
    days = int((time.time() - ts) // 86400)
    if days <= 0:
        return "сьогодні"
    if days == 1:
        return "вчора"
    return f"{plural(days, 'день', 'дні', 'днів')} тому"


SALES_PER_PAGE = 8


def _sales_text(watch, sold, show_account_hint=False, page=0):
    """Статистика продажів товару за конфігураціями (з таблиці спостережень, без запитів до eBay)."""
    title = f"📈 <b>{html.escape(watch['label'])}: продажі за {SOLD_LOOKBACK_DAYS} днів</b>"
    if not sold:
        text = (f"{title}\n\nПродажів ще не помічено. Бот вважає оголошення проданим, коли воно зникає "
                "з eBay задовго до кінця строку (або eBay підтверджує продаж). Статистика "
                "накопичується з кожним ринковим скануванням — зазирни через день-два.")
        return text + ("\n\n💡 Підключи «🔐 Акаунт eBay» в меню — тоді бот перевірятиме кожен продаж через eBay."
                       if show_account_hint else "")

    week_ago = time.time() - 7 * 86400
    confirmed = sum(1 for r in sold if r["sold_check"] == "sold")
    week = sum(1 for r in sold if r["gone_at"] >= week_ago)
    lines = [
        title, "",
        f"Продано: <b>{len(sold)}</b>" + (f" (✅ підтверджено eBay: {confirmed})" if confirmed else ""),
        f"За останні 7 днів: <b>{week}</b> (~{week / 7:.1f} на день)",
    ]

    groups = {}
    for r in sold:
        groups.setdefault((r["cond_group"], r["spec_group"] or "unspecified"), []).append(r)
    current_cond = None
    for (cond, spec), rows in sorted(groups.items(), key=lambda kv: (kv[0][0], -len(kv[1]))):
        if cond != current_cond:
            current_cond = cond
            lines.append(f"\n<b>{html.escape(CONDITION_LABELS.get(cond, cond).capitalize())}</b>")
        prices = [r["price"] for r in rows]
        clean = filter_outliers(prices) or prices
        spec_txt = "конфігурація не вказана" if spec == "unspecified" else spec
        n_confirmed = sum(1 for r in rows if r["sold_check"] == "sold")
        lines.append(
            f"• <b>{html.escape(spec_txt)}</b> — {plural(len(rows), 'продаж', 'продажі', 'продажів')}"
            + (f" (✅{n_confirmed})" if n_confirmed else "")
            + (f"\n  💶 типова ціна <b>{statistics.median(clean):.0f}€</b>, {min(prices):.0f}–{max(prices):.0f}€"
               if min(prices) != max(prices) else f"\n  💶 ціна <b>{prices[0]:.0f}€</b>")
            + (f"\n  ⏱ продається {speed_text(summarize(rows)['median_days'])}"
               if summarize(rows)["median_days"] is not None else "")
            + f"\n  🕒 останній {_ago(rows[0]['gone_at'])}"
        )

    pages = max(1, -(-len(sold) // SALES_PER_PAGE))
    page = min(page, pages - 1)
    lines.append("\n<b>Останні продажі</b>" + (f" (стор. {page + 1}/{pages})" if pages > 1 else "") + ":")
    first = page * SALES_PER_PAGE
    for n, r in enumerate(sold[first:first + SALES_PER_PAGE], first + 1):
        date = datetime.fromtimestamp(r["gone_at"], LOCAL_TZ).strftime("%d.%m")
        spec = "" if (r["spec_group"] or "unspecified") == "unspecified" else f" · {r['spec_group']}"
        mark = {"sold": "✅ ", "pending": "⏳ "}.get(r["sold_check"], "")
        name = html.escape((r["title"] or "оголошення")[:45])
        link = f'<a href="{html.escape(r["url"])}">{name}</a>' if r["url"] else name
        lines.append(f"{n}. {mark}{date}{html.escape(spec)} · <b>{r['price']:.0f}€</b> — {link}")

    lines.append("\n<i>Ціна — з доставкою, остання, яку бачив бот. ✅ — продаж підтвердив eBay; "
                 "⏳ — ще в черзі на перевірку; без позначки — eBay не відповів, оцінка за "
                 "зникненням (оголошення зникло задовго до кінця строку). "
                 "Чужий товар у списку — натисни ❌ з його номером, і він більше не впливатиме на ціни.</i>")
    if show_account_hint:
        lines.append("<i>💡 Підключи «🔐 Акаунт eBay» в меню — тоді бот відсіюватиме оголошення, "
                     "які продавець просто зняв.</i>")
    return "\n".join(lines)


def _sales_keyboard(watch_id, sold, page, extra_rows=()):
    pages = max(1, -(-len(sold) // SALES_PER_PAGE))
    page = min(page, pages - 1)
    first = page * SALES_PER_PAGE
    buttons = [InlineKeyboardButton(f"❌ {n}", callback_data=f"srej:{watch_id}:{page}:{r['item_id']}")
               for n, r in enumerate(sold[first:first + SALES_PER_PAGE], first + 1)]
    rows = list(extra_rows) + [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Новіші", callback_data=f"sales:{watch_id}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Старіші ▶️", callback_data=f"sales:{watch_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")])
    return InlineKeyboardMarkup(rows)


async def _show_sales(update, context, watch, page=0, note="", extra_rows=()):
    sold = get_sold_listings(watch["id"])
    hint = is_owner(update.effective_user.id) and not is_connected()
    text = _sales_text(watch, sold, show_account_hint=hint, page=page)
    pending = watch_obs_summary(watch["id"])["pending"] if is_connected() else 0
    if pending:
        text += (f"\n\n⏳ Ще перевіряються через eBay: <b>{pending}</b> — у статистику потраплять, "
                 "лише коли eBay підтвердить продаж.")
    if note:
        text = f"{note}\n\n{text}"
    await show_panel(update, context, text, reply_markup=_sales_keyboard(watch["id"], sold, page, extra_rows),
                     parse_mode=ParseMode.HTML)


@require_access
async def sales_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """sales:<id>[:<сторінка>] — статистика продажів товару за конфігураціями."""
    query_cb = update.callback_query
    parts = query_cb.data.split(":")
    watch = get_watch(int(parts[1]), update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    await _ack_callback(update)
    await _show_sales(update, context, watch, page=int(parts[2]) if len(parts) > 2 else 0)


@require_access
async def sales_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """srej:<watch_id>:<сторінка>:<item_id> — прибрати чужий лот зі статистики продажів."""
    query_cb = update.callback_query
    _, watch_id, page, item_id = query_cb.data.split(":", 3)
    watch = get_watch(int(watch_id), update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    row = next((r for r in get_sold_listings(watch["id"]) if r["item_id"] == item_id), None)
    if row is None:
        await query_cb.answer("Цього продажу вже немає в списку.")
        return await _show_sales(update, context, watch, int(page))
    # «Не той товар»: більше не враховується ні в продажах, ні в цінах, ні в пошуку;
    # якщо в таких назвах повторюються слова — бот їх вивчить (як ❌ Інший товар)
    words = await asyncio.to_thread(reject_and_learn, watch, item_id, row["title"])
    if words:
        # Продажі з щойно вивченими словами теж чужі — прибираємо й їх
        word_set = set(words)
        stale = [r["item_id"] for r in get_sold_listings(watch["id"])
                 if _search_tokens(r.get("title") or "") & word_set]
        delete_listing_obs_by_ids(watch["id"], stale)
    await asyncio.to_thread(refresh_sale_prices, watch["id"])
    await query_cb.answer("❌ Прибрано — на ціни більше не впливає")
    note = f"❌ Прибрано: {html.escape((row['title'] or '')[:60])}"
    extra = []
    if words:
        note += "\n" + html.escape(learned_words_note(words))
        extra = [[InlineKeyboardButton(f"↩️ Не відсіювати «{w}»", callback_data=f"unlw:{watch['id']}:{w}")]
                 for w in words]
    await _show_sales(update, context, watch, int(page), note=note, extra_rows=extra)


@require_access
async def config_listings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """10 найдешевших поточних оголошень обраної групи (з останнього
    ринкового сканування — без додаткових запитів до eBay)."""
    query_cb = update.callback_query
    _, watch_id_str, idx_str = query_cb.data.split(":")
    watch_id = int(watch_id_str)
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    groups = context.user_data.get(f"cfg_groups_{watch_id}") or []
    try:
        cond, spec = groups[int(idx_str)]
    except (ValueError, IndexError):
        await query_cb.answer("Список застарів — відкрий «Усі конфігурації» знову.", show_alert=True)
        return

    rows_db = [
        r for r in get_current_listings(watch_id)
        if r["cond_group"] == cond and (spec == "*" or r["spec_group"] == spec) and r.get("url")
    ]
    rows_db.sort(key=lambda r: r["price"])
    top = rows_db

    spec_txt = "усі конфігурації" if spec == "*" else ("без конфігурації" if spec == "unspecified" else spec)
    header = (f"🔎 <b>{html.escape(watch['label'])}</b>\n"
              f"{html.escape(CONDITION_LABELS.get(cond, cond).capitalize())} · {html.escape(spec_txt)}")
    nav = [
        [InlineKeyboardButton("◀️ До конфігурацій", callback_data=f"configs:{watch_id}")],
        [InlineKeyboardButton("📌 До товару", callback_data=f"watch_details:{watch_id}")],
    ]
    if not top:
        await show_panel(
            update, context,
            header + "\n\nПосилань на оголошення ще немає — вони з'являться після наступного "
            "ринкового сканування (до години).",
            reply_markup=InlineKeyboardMarkup(nav), parse_mode=ParseMode.HTML,
        )
        return

    state = {
        "header": header,
        "items": [
            {"item_id": r["item_id"], "title": r["title"] or "без назви", "price": r["price"],
             "currency": "€", "condition": None, "url": r["url"]}
            for r in top
        ],
        "nav": [("◀️ До конфігурацій", f"configs:{watch_id}"), ("📌 До товару", f"watch_details:{watch_id}")],
        "query": watch["query"],
    }
    await _render_listing_panel(update, context, watch_id, state)


async def _render_aspect_picker(update, context, watch):
    watch_id = watch["id"]
    options = context.user_data.get(f"asp_options_{watch_id}") or []
    selected = context.user_data.get(f"asp_selected_{watch_id}") or set()
    current = ", ".join(get_required_aspects(watch)) or (
        "авто" if watch.get("require_spec") is None else "без вимоги")
    back_row = [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")]
    await show_panel(
        update, context,
        f"🧾 <b>{html.escape(watch['label'])}: обов'язкові характеристики</b>\n"
        f"Зараз: {html.escape(current)}\n\n{ASPECT_CHOICE_TEXT}",
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
    reset_watch_market(watch_id)  # інший фільтр лотів — ринок аналізуємо заново
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
    reset_watch_market(watch_id)
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
        + "\nПісля збереження ринок буде проаналізовано з нуля.",
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
    if choice == "all":
        update_watch_categories(watch_id, chat_id, [])
    elif choice == "done":
        if not selected:
            await query_cb.answer("Познач хоча б одну категорію або обери «Усі категорії».", show_alert=True)
            return
        update_watch_categories(
            watch_id, chat_id, [{"id": o["id"], "name": o["name"]} for o in options if o["id"] in selected],
        )
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
    # Характеристики різних категорій різні — повертаємо автоматичний режим
    update_watch_required_aspect(watch_id, chat_id, None, None)
    # Інша категорія — інша вибірка, стара статистика вже не відповідає
    reset_watch_market(watch_id)
    await _show_watch_details(update, context, get_watch(watch_id, chat_id))


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
        lines.append(f"<b>{i}. {html.escape(it['title'][:160])}</b>\n"
                     f"💶 {it['price']:.0f} {html.escape(it['currency'])}{cond}")
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
        [InlineKeyboardButton("🗂️ Змінити категорії", callback_data=f"chcat:{watch_id}")],
        [InlineKeyboardButton(
            f"💶 Мінімальна ціна ({f'{min_price:.0f}€' if min_price else 'авто'})",
            callback_data=f"edmin:{watch_id}")],
        [InlineKeyboardButton("🧾 Обов'язкові характеристики", callback_data=f"reqasp:{watch_id}")],
        [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")],
    ]
    await show_panel(
        update, context,
        f"✏️ <b>{html.escape(watch['label'])}</b>\n\nЩо хочеш відредагувати?",
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
    reset_watch_market(watch_id)  # інша вибірка — ринок аналізуємо заново

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


# ============================================================
# «⚙️ МІНІМАЛЬНИЙ ПРИБУТОК» — з якого прибутку пропозиція вважається вигідною
# ============================================================

@require_access
async def min_profit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mprof:show — екран вибору; mprof:<сума> — зберегти."""
    await _ack_callback(update)
    chat_id = update.effective_chat.id
    choice = update.callback_query.data.split(":")[1]
    note = ""
    if choice != "show":
        set_min_profit(chat_id, float(choice))
        note = f"✅ Збережено: пропозиції з прибутком від <b>{float(choice):.0f}€</b>\n\n"
    current = get_min_profit(chat_id)
    buttons = [InlineKeyboardButton(("✅ " if v == current else "") + f"{v}€", callback_data=f"mprof:{v}")
               for v in MIN_PROFIT_CHOICES]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:home")])
    await show_panel(
        update, context,
        f"{note}⚙️ <b>Мінімальний прибуток</b>: зараз <b>{current:.0f}€</b>\n\n"
        "Бот пропонує лише оголошення, на яких після перепродажу (мінус комісія eBay і доставка) "
        "лишається щонайменше стільки. Від цього залежить і «купувати до» в картках товарів.\n\n"
        "Прибуток рахується за ціною оголошення — можливість торгуватися (🎯) лише бонус. "
        "Зміна одразу прибирає зі «🔥 Вигідні пропозиції» все, що нижче нового порогу.",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
    )


# ============================================================
# «🔥 ВИГІДНІ ПРОПОЗИЦІЇ» — усі знахідки в одному місці замість потоку повідомлень
# ============================================================

DEALS_PER_PAGE = 5


def _when(ts):
    moment = datetime.fromtimestamp(ts, LOCAL_TZ)
    days = (datetime.now(LOCAL_TZ).date() - moment.date()).days
    if days == 0:
        return f"сьогодні о {moment:%H:%M}"
    if days == 1:
        return f"вчора о {moment:%H:%M}"
    return f"{moment:%d.%m} о {moment:%H:%M}"


def _deal_card(n, d, sold=None):
    sale = d["median_price"]
    note = sales_note(sold, d["cond_group"], d["spec_group"]) if sold and d.get("cond_group") else ""
    _, profit = estimate_resale_profit(sale, d["total_price"])
    new = "🆕 " if d["seen_at"] is None else ""
    offer = " · 🎯 можна торгуватись" if d["has_best_offer"] else ""
    warn = "\n⚠️ Мало відгуків у продавця — перевір уважно" if d["suspicious"] else ""
    return (f"<b>{n}. {new}{html.escape(d['watch_label'])}</b> · {_when(d['created_at'])}\n"
            f"{html.escape(d['title'][:90])}\n"
            f"💶 <b>{d['total_price']:.0f}€</b> → продати ~{sale:.0f}€ · 💰 прибуток ~<b>{profit:.0f}€</b>{offer}{warn}\n"
            + (f"{html.escape(note)}\n" if note else "") +
            f'<a href="{html.escape(d["url"])}">🔗 Відкрити на eBay</a>')


async def _render_deals(update, context, page=0, note=""):
    chat_id = update.effective_chat.id
    deals, total = get_inbox_deals(chat_id, limit=DEALS_PER_PAGE, offset=page * DEALS_PER_PAGE)
    if not deals and page > 0:
        page = max(0, (total - 1) // DEALS_PER_PAGE)
        deals, total = get_inbox_deals(chat_id, limit=DEALS_PER_PAGE, offset=page * DEALS_PER_PAGE)
    lines = [f"🔥 <b>Вигідні пропозиції</b> ({total})"]
    if note:
        lines.append(note)
    rows = []
    if not deals:
        lines.append("Поки нових немає. Щойно бот знайде вигідну пропозицію — прийде одне коротке "
                     "сповіщення, а сама пропозиція з'явиться тут.")
    sold_by_watch = {}
    for i, d in enumerate(deals, 1 + page * DEALS_PER_PAGE):
        if d["watch_id"] not in sold_by_watch:
            sold_by_watch[d["watch_id"]] = get_sold_listings(d["watch_id"], SALES_WINDOW_DAYS)
        lines.append(_deal_card(i, d, sold_by_watch[d["watch_id"]]))
        rows.append([
            InlineKeyboardButton(f"✅ {i} Куплено", callback_data=f"dact:buy:{d['id']}:{page}"),
            InlineKeyboardButton(f"🙈 {i}", callback_data=f"dact:hide:{d['id']}:{page}"),
            InlineKeyboardButton(f"❌ {i} Інший товар", callback_data=f"dact:rej:{d['id']}:{page}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Новіші", callback_data=f"deals:{page - 1}"))
    if (page + 1) * DEALS_PER_PAGE < total:
        nav.append(InlineKeyboardButton("Старіші ▶️", callback_data=f"deals:{page + 1}"))
    if nav:
        rows.append(nav)
    if total:
        rows.append([InlineKeyboardButton("🧹 Очистити список", callback_data="dclear")])
    rows.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:home")])
    if deals:
        lines.append("<i>🙈 — сховати це оголошення; ❌ Інший товар — не той товар, бот запам'ятає.</i>")
    await show_panel(update, context, "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
                     parse_mode=ParseMode.HTML)
    mark_deals_seen([d["id"] for d in deals])
    # Сповіщення «нова пропозиція» вже не потрібне — прибираємо його з чату
    notice = context.bot_data.get("deal_notice", {}).pop(chat_id, None)
    if notice and notice.get("message_id"):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=notice["message_id"])
        except Exception:
            pass


@require_access
async def deals_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """deals:<сторінка> — список вигідних пропозицій."""
    await _ack_callback(update)
    try:
        page = int(update.callback_query.data.split(":")[1])
    except (IndexError, ValueError):
        page = 0
    await _render_deals(update, context, page)


@require_access
async def deal_inbox_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dact:<buy|hide|rej>:<deal_id>:<сторінка>"""
    query_cb = update.callback_query
    _, action, deal_id, page = query_cb.data.split(":")
    deal = get_deal(int(deal_id))
    watch = get_watch(deal["watch_id"], update.effective_chat.id) if deal else None
    if watch is None:
        await query_cb.answer("Цієї пропозиції вже немає.", show_alert=True)
        return await _render_deals(update, context, int(page))
    note, words = "", []
    if action == "buy":
        set_deal_status(deal["id"], "bought")
        await query_cb.answer("✅ Позначено як куплене")
        note = f"✅ «{html.escape(deal['title'][:50])}» — куплено"
    elif action == "hide":
        await asyncio.to_thread(hide_item, watch, deal["item_id"], deal["title"])
        set_deal_status(deal["id"], "skipped")
        await query_cb.answer("🙈 Сховано")
    else:
        words = await asyncio.to_thread(reject_and_learn, watch, deal["item_id"], deal["title"])
        set_deal_status(deal["id"], "skipped")
        await query_cb.answer("❌ Прибрано — більше не враховую це оголошення")
        if words:
            note = html.escape(learned_words_note(words))
    await _render_deals(update, context, int(page), note=note)


@require_access
async def deals_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ack_callback(update)
    clear_inbox_deals(update.effective_chat.id)
    await _render_deals(update, context, 0, note="🧹 Список очищено")


# ============================================================
# «💡 ЩО ПЕРЕПРОДАВАТИ»
# ============================================================

def _recommendation_text(n, r):
    spec = f" ({html.escape(r['spec'])})" if r.get("spec") else ""
    per_week = r["deals_per_week"]
    if per_week >= 1:
        deals = f"🔥 ~{per_week:.0f} вигідних на тиждень, прибуток ~{r['deal_profit']}€ з кожної"
    else:
        deals = f"🔥 вигідні трапляються рідко (зараз {r['deals_now']}), прибуток ~{r['deal_profit']}€"
    sold_week = r.get("sold_week") or 0
    if sold_week:
        confirmed = f" (✅{r['sold_confirmed']})" if r.get("sold_confirmed") else ""
        speed = f", продаються {speed_text(r['sold_days'])}" if r.get("sold_days") is not None else ""
        price = f" по ~{r['sold_median']}€" if r.get("sold_median") else ""
        sold = f"🛒 продано за тиждень: {sold_week}{confirmed}{price}{speed}"
    elif r["sold_per_day"] is None:
        sold = "🛒 продажі ще рахую (потрібно кілька перевірок)"
    else:
        sold = "🛒 за тиждень продажів не помічено"
    sale_note = " (за продажами)" if r.get("sale_source") == "sold" else ""
    return (f"<b>{n}. {r['emoji']} {html.escape(r['name'])}</b>{spec}\n"
            f"💶 типова ціна ~{r['median']}€ · купувати до ~{r['buy_limit']}€{sale_note}\n"
            f"{deals}\n{sold}")


@require_access
async def discover_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, note=""):
    """menu:discover — рейтинг товарів, які варто перепродавати."""
    recs = top_recommendations(update.effective_chat.id)
    context.user_data["discover_recs"] = [
        {"name": r["name"], "query": r["query"], "floor": r["floor"]} for r in recs
    ]
    lines = ["💡 <b>Що варто перепродавати</b>",
             "<i>Бот сам аналізує популярні товари на eBay.de, яких ти ще не відстежуєш, "
             "і ставить угорі ті, на яких найреальніше заробити.</i>"]
    if note:
        lines.append(note)
    rows = []
    if recs:
        updated = max(r["updated_at"] for r in recs)
        lines.append(f"<i>Оновлено: {datetime.fromtimestamp(updated, LOCAL_TZ).strftime('%d.%m %H:%M')}</i>")
        lines += [_recommendation_text(n, r) for n, r in enumerate(recs, 1)]
        lines.append("<i>Продажі — оголошення, які зникли з eBay задовго до кінця строку; ✅ — продаж "
                     "підтвердив eBay. «За продажами» — ціна рахується за ними, інакше — за оголошеннями. "
                     "Натисни товар, щоб почати його відстежувати.</i>")
        buttons = [InlineKeyboardButton(f"➕ {r['name']}", callback_data=f"dadd:{i}") for i, r in enumerate(recs)]
        rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    else:
        lines.append("Аналіз ще не готовий: бот перевіряє популярні товари раз на 4 години, "
                     "перший результат з'явиться протягом кількох хвилин після запуску.")
    if is_owner(update.effective_user.id):
        rows.append([InlineKeyboardButton("🔄 Оновити аналіз", callback_data="drefresh")])
    rows.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:home")])
    await show_panel(update, context, "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
                     parse_mode=ParseMode.HTML)


@require_access
async def discover_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """drefresh — перерахувати рейтинг зараз (лише власник: ~90 запитів до eBay)."""
    if not is_owner(update.effective_user.id):
        return await discover_callback(update, context)
    await show_panel(update, context, "💡 ⏳ Аналізую ~90 популярних товарів на eBay… (1–2 хвилини)")
    try:
        await asyncio.to_thread(run_discovery, True)
    except Exception as e:
        log.exception("Не вдалося оновити «Що перепродавати»: %s", e)
    await discover_callback(update, context)


@require_access
async def discover_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dadd:<індекс> — почати відстежувати рекомендований товар."""
    query_cb = update.callback_query
    recs = context.user_data.get("discover_recs") or []
    try:
        rec = recs[int(query_cb.data.split(":")[1])]
    except (ValueError, IndexError):
        await query_cb.answer("Список застарів — відкрий «💡 Що перепродавати» знову.", show_alert=True)
        return
    chat_id = update.effective_chat.id
    existing = find_duplicate_watch(chat_id, rec["query"])
    if existing:
        await _show_watch_details(update, context, existing)
        return
    wid = add_watch(
        chat_id=chat_id, label=rec["name"], query=rec["query"],
        exclude="broken defekt teile parts kaputt", condition_ids=DEFAULT_CONDITION_IDS,
        discount_threshold_pct=DEFAULT_DISCOUNT_THRESHOLD_PCT, categories=[],
        min_price=rec["floor"], require_spec=None, required_aspect=None,
    )
    watch = get_watch(wid, chat_id)
    await show_panel(update, context, f"✅ «{html.escape(rec['name'])}» додано.\n\n⏳ Рахую ціни на eBay…",
                     parse_mode=ParseMode.HTML)
    try:
        await asyncio.to_thread(attach_shared_history, watch)
    except Exception as e:
        log.warning("Не вдалося підключити спільні дані ринку для «%s»: %s", rec["name"], e)
    try:
        await _recalculate_watch_medians(watch, replace_existing=True)
    except Exception as e:
        log.warning("Не вдалося одразу порахувати ціни для «%s»: %s", rec["name"], e)
    await _show_watch_details(update, context, get_watch(wid, chat_id))


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


async def edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("edit", None)
    await show_main_menu(update, context)
    return ConversationHandler.END


@require_access
async def recalculate_median_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    chat_id = update.effective_chat.id
    watch = get_watch(watch_id, chat_id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return

    await show_panel(update, context, f"🔄 <b>{html.escape(watch['label'])}</b>\n\n⏳ Оновлюю ціни з eBay…",
                     parse_mode=ParseMode.HTML)
    try:
        items, medians, _ = await _recalculate_watch_medians(watch, replace_existing=True)
    except Exception as e:
        log.exception("Не вдалося перерахувати медіану для watch #%s: %s", watch_id, e)
        await _notify_median_error(context.application, watch, e)
        await _show_watch_details(update, context, get_watch(watch_id, chat_id))
        return

    if not items:
        await _notify_median_problem(
            context.application,
            watch,
            "eBay не повернув жодного оголошення, яке відповідає фільтрам цього товару.",
        )
    elif not medians:
        minimum = minimum_sample_size_for_query(watch["query"])
        await _notify_median_problem(
            context.application,
            watch,
            f"Знайдено {plural(len(items), 'оголошення', 'оголошення', 'оголошень')}, але після фільтрації "
            f"їх замало, щоб порахувати ціни. Потрібно щонайменше "
            f"{plural(minimum, 'оголошення', 'оголошення', 'оголошень')} одного стану й конфігурації.",
        )

    await _show_watch_details(update, context, get_watch(watch_id, chat_id))


async def refresh_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """menu:refresh_prices («💰 Оновити ціни», лише власник): свіжі ціни з eBay для
    всіх товарів власника (+ свіжі дані про ліміт запитів)."""
    if not is_owner(update.effective_user.id):
        await _ack_callback(update)
        await show_main_menu(update, context)
        return
    if context.bot_data.get("refresh_all_running"):
        try:
            await update.callback_query.answer("Уже оновлюю — зачекай кілька секунд.")
        except Exception:
            pass
        return
    await _ack_callback(update)
    chat_id = update.effective_chat.id
    watches = list_watches(chat_id=chat_id, active_only=True)
    done, failed, skipped = [], [], []
    context.bot_data["refresh_all_running"] = True
    try:
        for n, watch in enumerate(watches, 1):
            if browse_budget_left() < SEARCH_RESERVE:
                skipped = [w["label"] for w in watches[n - 1:]]
                break
            await show_panel(
                update, context,
                f"🔄 Оновлюю ціни з eBay… ({n}/{len(watches)})\n\n⏳ <b>{html.escape(watch['label'])}</b>",
                parse_mode=ParseMode.HTML,
            )
            try:
                _, stats, _ = await _recalculate_watch_medians(watch, replace_existing=True)
                (done if stats else failed).append(watch["label"])
            except Exception as e:
                log.warning("Не вдалося оновити ціни для watch #%s: %s", watch["id"], e)
                failed.append(watch["label"])
        try:
            await asyncio.to_thread(fetch_browse_rate_limit)
        except Exception as e:
            log.warning("Не вдалося оновити дані про ліміт eBay: %s", e)
    finally:
        context.bot_data["refresh_all_running"] = False

    notes = []
    if done:
        notes.append(f"✅ Ціни оновлено: {plural(len(done), 'товар', 'товари', 'товарів')}")
    if failed:
        notes.append("⚠️ Не вдалося порахувати: " + html.escape(", ".join(failed)))
    if skipped:
        notes.append("⏸ Замало запитів до eBay на сьогодні, не оновлено: " + html.escape(", ".join(skipped)))
    if not watches:
        notes.append("Товарів ще немає — оновлено лише дані про запити.")
    user_id = update.effective_user.id
    await show_panel(update, context, main_menu_text(user_id) + "\n\n" + "\n".join(notes),
                     reply_markup=build_main_menu(user_id), parse_mode=ParseMode.HTML)


@require_access
async def delwatch_ask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """delwatch_ask:<id> — підтвердження видалення з картки товару."""
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    await _ack_callback(update)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Так, видалити", callback_data=f"delwatch_yes:{watch_id}"),
        InlineKeyboardButton("❌ Ні", callback_data=f"watch_details:{watch_id}"),
    ]])
    await show_panel(
        update, context,
        f"🗑️ Видалити товар «{html.escape(watch['label'])}»?",
        reply_markup=keyboard, parse_mode=ParseMode.HTML,
    )


@require_access
async def delwatch_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wid = int(update.callback_query.data.split(":")[1])
    chat_id = update.effective_chat.id
    remove_watch(wid, chat_id)
    await _ack_callback(update)
    await cmd_list(update, context)


@require_access
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    stats = get_deal_stats(chat_id)
    if not stats:
        await show_panel(update, context, "Поки що немає жодної вигідної пропозиції.", reply_markup=back_to_menu_keyboard())
        return
    bought = stats.get("bought", 0)
    skipped = stats.get("skipped", 0)
    new = stats.get("new", 0)
    await show_panel(
        update, context,
        f"📊 Твоя статистика\n\n✅ Куплено: {bought}\n❌ Пропущено: {skipped}\n🆕 Ще не позначено: {new}",
        reply_markup=back_to_menu_keyboard(),
    )


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