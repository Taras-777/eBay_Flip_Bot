"""
Екрани й діалоги бота: додавання, перегляд, редагування й видалення товарів,
команди користувача.
"""

import asyncio
import config
import html
import statistics
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from settings import (
    CONDITION_PRESETS,
    DEFAULT_CONDITION_IDS,
    MAX_ALLOWED_THRESHOLD_PCT,
    MAX_SPEC_LOOKUPS_PER_DEAL_SCAN,
    MIN_ALLOWED_THRESHOLD_PCT,
    MIN_PRICE_SUGGESTION_PCT,
    MIN_PROFIT_EUR,
    MIN_SAMPLE_SIZE,
    MIN_SOLD_SAMPLE,
    is_owner,
    log,
)
from textparse import CONDITION_LABELS, _group_label, category_label, is_accessory_category
from db import (
    add_watch,
    encode_required_aspects,
    find_duplicate_watch,
    find_threshold_suggestion,
    get_current_listings,
    get_deal_owner_chat_id,
    get_deal_stats,
    get_market_stats,
    get_required_aspects,
    get_user_row,
    get_watch,
    get_watch_categories,
    list_watches,
    remove_watch,
    reset_watch_market,
    set_deal_status,
    update_threshold,
    update_watch_categories,
    update_watch_conditions,
    update_watch_exclude,
    update_watch_min_price,
    update_watch_require_spec,
    update_watch_required_aspect,
    upsert_user_request,
    watch_category_ids,
)
from ebay_api import (
    _watch_search_kwargs,
    aspect_options_for_categories,
    get_category_options,
    search_in_categories,
)
from market import (
    _annotate_items,
    _apply_item_filters,
    _recalculate_watch_medians,
    analyze_market_for_threshold,
    estimate_resale_profit,
    max_buy_price,
    minimum_sample_size_for_query,
    suggest_min_price,
    watch_requires_spec,
)
from panel import (
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


# ============================================================
# ДІАЛОГ ДОДАВАННЯ ВІДСТЕЖЕННЯ (/addwatch)
# Кроки: назва → категорія eBay → мінімальна ціна → поріг знижки
# ============================================================

(
    ASK_QUERY,
    ASK_CATEGORY,
    ASK_ASPECT,
    ASK_MIN_PRICE_CHOICE,
    ASK_CUSTOM_MIN_PRICE,
    ASK_THRESHOLD_CHOICE,
    ASK_CUSTOM_THRESHOLD,
) = range(7)


NEW_WATCH_KEYS = (
    "new_watch_query", "suggested_pct", "new_watch_category_id", "new_watch_category_name",
    "new_watch_categories", "new_watch_category_selected",
    "new_watch_category_options", "new_watch_min_price", "suggested_min_price",
    "new_watch_aspect_options", "new_watch_required_aspect", "new_watch_require_spec",
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
    "Познач одну або кілька характеристик і натисни «💾 Зберегти». Лот враховуватиметься, "
    "лише якщо в нього заповнені ВСІ позначені характеристики — інакше вважається аксесуаром.\n"
    "❗ — обов'язкова в цій категорії (продавець не може її пропустити).\n"
    "🤖 Авто — пам'ять для телефонів, ноутбуків і консолей; 🚫 — без вимоги.\n\n"
    "⚠️ Якщо характеристик зазвичай немає в назві, бот перевірятиме характеристики кожного "
    "оголошення — це додаткові запити до eBay (з кешем і лімітом). "
    "Лоти з «Kompatible Marke/Modell» відкидаються завжди."
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
        await show_panel(update, context, "Порожній запит не підходить. Спробуй ще раз.", reply_markup=cancel_keyboard())
        return ASK_QUERY

    chat_id = update.effective_chat.id
    duplicate = find_duplicate_watch(chat_id, query)
    if duplicate:
        await show_panel(
            update, context,
            f"⚠️ У тебе вже є відстеження #{duplicate['id']} «{html.escape(duplicate['query'])}» "
            f"з такою ж назвою (поріг {duplicate['discount_threshold_pct']}%).\n\n"
            f"Введи іншу назву — наприклад, додай конкретний обсяг пам'яті чи стан, "
            f"щоб відрізнити від наявного запису.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_QUERY

    context.user_data["new_watch_query"] = query

    if not _ebay_configured():
        return await _propose_threshold(update, context)

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
        return await _propose_min_price(update, context)
    try:
        cat_id = options[int(choice)]["id"]
    except (ValueError, IndexError):
        return ASK_CATEGORY
    selected.symmetric_difference_update({cat_id})
    await _render_new_watch_categories(update, context)
    return ASK_CATEGORY


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
        return await _propose_threshold(update, context)

    min_price, median_price, sample_size = suggestion
    context.user_data["suggested_min_price"] = min_price
    cat_line = f"Категорія: {html.escape(category_name)}\n" if category_name else ""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ Від {min_price:.0f}€", callback_data="minp:use"),
            InlineKeyboardButton("✏️ Своя ціна", callback_data="minp:custom"),
        ],
        [InlineKeyboardButton("Без обмеження", callback_data="minp:none")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="menu:home")],
    ])
    await show_panel(
        update, context,
        f"💶 <b>«{html.escape(query)}»: мінімальна ціна</b>\n\n"
        f"{cat_line}"
        f"Медіана зараз: ~{median_price:.0f}€ (за {sample_size} оголошеннями).\n\n"
        f"Пропоную не враховувати лоти дешевші за <b>{min_price:.0f}€</b> "
        f"(~{MIN_PRICE_SUGGESTION_PCT}% від медіани) — так відсіюються ігри, "
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
            "✏️ Введи мінімальну ціну в євро (напр. 120), або 0 — без обмеження.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_CUSTOM_MIN_PRICE
    if choice == "use":
        context.user_data["new_watch_min_price"] = context.user_data.get("suggested_min_price", 0)
    else:
        context.user_data["new_watch_min_price"] = 0
    return await _propose_threshold(update, context)


async def addwatch_custom_min_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace("€", "").replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        await show_panel(update, context, "Це не схоже на число. Введи, наприклад: 120", reply_markup=cancel_keyboard())
        return ASK_CUSTOM_MIN_PRICE
    if value < 0:
        await show_panel(update, context, "Ціна не може бути відʼємною. Спробуй ще раз.", reply_markup=cancel_keyboard())
        return ASK_CUSTOM_MIN_PRICE
    context.user_data["new_watch_min_price"] = value
    return await _propose_threshold(update, context)


async def _propose_threshold(update, context):
    query = context.user_data["new_watch_query"]
    category_ids = [c["id"] for c in context.user_data.get("new_watch_categories") or []]
    min_price = context.user_data.get("new_watch_min_price") or None

    analysis = None
    validation_note = ""
    if _ebay_configured():
        await show_panel(
            update, context,
            f"🔎 «{html.escape(query)}»\n\nПеревіряю поточні ціни на eBay, зачекай кілька секунд…",
            reply_markup=cancel_keyboard(),
        )
        analysis = await asyncio.to_thread(analyze_market_for_threshold, query, category_ids, min_price)
        if analysis is None:
            validation_note = (
                "\n\n⚠️ Не вдалося зібрати достатньо оголошень для аналізу ринку — "
                "поріг нижче запропоновано за загальною підказкою, а не за реальними цінами."
            )
    else:
        validation_note = "\n\n(аналіз ринку на eBay недоступний, поки не додані API-ключі — поріг за загальною підказкою)"

    if analysis:
        pct = analysis["pct"]
        reason = analysis["reason"]
        spec_warning = analysis.get("spec_warning")
        if spec_warning:
            validation_note += f"\n\n💡 {html.escape(spec_warning)}"
    else:
        pct, reason = find_threshold_suggestion(query)

    context.user_data["suggested_pct"] = pct

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"✅ Взяти {pct}%", callback_data="use_suggested"),
                InlineKeyboardButton("✏️ Своє значення", callback_data="use_custom"),
            ],
            [InlineKeyboardButton("❌ Скасувати", callback_data="menu:home")],
        ]
    )
    await show_panel(
        update, context,
        f"🔎 «{html.escape(query)}»\n\n"
        f"💡 Пропоную бажаний прибуток: {pct}% від ціни продажу (мінімум {MIN_PROFIT_EUR}€)\n"
        f"Причина: {html.escape(reason)}\n"
        f"Бот вважатиме лот вигідним, якщо після перепродажу (мінус комісія eBay і доставка) "
        f"лишається щонайменше цей прибуток"
        f"{validation_note}\n\n"
        f"Це лише рекомендація — остаточне рішення за тобою.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )
    return ASK_THRESHOLD_CHOICE


async def addwatch_threshold_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    await query_cb.answer()

    if query_cb.data == "use_suggested":
        pct = context.user_data["suggested_pct"]
        return await _finalize_watch(update, context, pct)

    await show_panel(
        update, context,
        f"✏️ Введи бажаний прибуток у відсотках від ціни продажу "
        f"(від {MIN_ALLOWED_THRESHOLD_PCT} до {MAX_ALLOWED_THRESHOLD_PCT}), напр: 30",
        reply_markup=cancel_keyboard(),
    )
    return ASK_CUSTOM_THRESHOLD


async def addwatch_custom_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace("%", "").replace(",", ".")
    try:
        pct = float(text)
    except ValueError:
        await show_panel(
            update, context, "Це не схоже на число. Введи, наприклад: 30",
            reply_markup=cancel_keyboard(),
        )
        return ASK_CUSTOM_THRESHOLD

    if not (MIN_ALLOWED_THRESHOLD_PCT <= pct <= MAX_ALLOWED_THRESHOLD_PCT):
        await show_panel(
            update, context,
            f"Значення має бути від {MIN_ALLOWED_THRESHOLD_PCT} до {MAX_ALLOWED_THRESHOLD_PCT}. Спробуй ще раз.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_CUSTOM_THRESHOLD

    return await _finalize_watch(update, context, pct)


async def _finalize_watch(update, context, pct):
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
        discount_threshold_pct=pct,
        categories=categories,
        min_price=min_price,
        require_spec=require_spec,
        required_aspect=required_aspect,
    )

    extras = []
    if category_name:
        extras.append(f"🗂️ Категорії: {html.escape(category_name)}")
    if min_price:
        extras.append(f"💶 Мінімальна ціна: {min_price:.0f}€")
    if required_aspect:
        extras.append(f"🧾 Обов'язкова характеристика: {html.escape(required_aspect)}")
    extras_txt = ("\n" + "\n".join(extras)) if extras else ""

    user_id = update.effective_user.id
    text = (
        f"✅ Додано відстеження #{wid}: «{html.escape(query)}», бажаний прибуток {pct}%."
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
    return ConversationHandler.END


# ============================================================
# КОМАНДИ КЕРУВАННЯ КАТЕГОРІЯМИ-ПІДКАЗКАМИ
# ============================================================


ASK_DELETE_ID = 0  # окрема невеличка "розмова": лише один крок — увести номер


@require_access
async def delwatch_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["delete_target"] = "watch"
    watches = list_watches(chat_id=update.effective_chat.id)
    if not watches:
        context.user_data.pop("delete_target", None)
        await show_panel(
            update, context,
            "Немає активних відстежень для видалення.",
            reply_markup=back_to_menu_keyboard(),
        )
        return ConversationHandler.END
    watch_lines = [f"#{w['id']} {html.escape(w['label'])}" for w in watches]
    await show_panel(
        update, context,
        "🗑️ <b>Видалення відстеження</b>\n\n"
        "Активні відстеження:\n"
        + "\n".join(watch_lines)
        + "\n\nВведи номер (#) товару, який хочеш видалити.",
        reply_markup=cancel_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ASK_DELETE_ID


async def got_delete_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lstrip("#")
    try:
        item_id = int(raw)
    except ValueError:
        await show_panel(
            update, context, "Це не схоже на номер. Введи число, наприклад: 3",
            reply_markup=cancel_keyboard(),
        )
        return ASK_DELETE_ID

    target = context.user_data.get("delete_target")
    chat_id = update.effective_chat.id

    if target == "watch":
        watch = get_watch(item_id, chat_id)
        if not watch:
            await show_panel(
                update, context,
                f"Товару #{item_id} немає серед активних відстежень. Спробуй ще раз.",
                reply_markup=cancel_keyboard(),
            )
            return ASK_DELETE_ID
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Так, видалити", callback_data=f"delwatch_yes:{item_id}"),
                InlineKeyboardButton("❌ Ні", callback_data="menu:list"),
            ]]
        )
        await show_panel(
            update, context,
            f"Видалити відстеження #{item_id} «{html.escape(watch['label'])}»?",
            reply_markup=keyboard,
        )
    else:
        await show_main_menu(update, context)

    context.user_data.pop("delete_target", None)
    return ConversationHandler.END


async def delete_menu_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка меню посеред введення номера для видалення — скасовує
    введення і одразу відкриває натиснутий розділ."""
    context.user_data.pop("delete_target", None)
    action = update.callback_query.data.split(":", 1)[1]
    if action == "list":
        await cmd_list(update, context)
    elif action == "stats":
        await cmd_stats(update, context)
    elif action == "pending":
        await cmd_pending(update, context)
    elif action == "users":
        await cmd_users(update, context)
    elif action == "addwatch":
        return await addwatch_start(update, context)
    else:
        await show_main_menu(update, context)
    return ConversationHandler.END


async def delete_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("delete_target", None)
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

    text = (
        "👋 Привіт! Я слідкую за цінами на eBay і сповіщаю, коли з'являється вигідний лот.\n\n"
        "Усе відбувається через кнопки меню нижче. Команди теж працюють:\n\n"
        "📦 Відстеження товарів\n"
        "/addwatch — додати новий товар (покроково)\n"
        "/list — активні відстеження\n"
        "/remove <id> — вимкнути відстеження\n"
        "/setthreshold <id> <%> — бажаний прибуток (% від ціни продажу)\n"
        "/setminprice <id> <€> — мінімальна ціна (0 — без обмеження)\n"
        "/setexclude <id> слова — виключені слова з пошуку\n"
        "/requirespec <id> <on|off|auto> — лише лоти з відомою пам'яттю\n"
        "/setconditions <id> <new|used|both> — які стани товару шукати\n"
        "\n"

        "📊 Статистика\n"
        "/stats — куплено/пропущено"
    )
    if is_owner(user.id):
        text += (
            "\n\n👑 Керування доступом (лише власник)\n"
            "/pending — запити, що очікують рішення\n"
            "/users — усі користувачі з доступом\n"
            "/userstats <id> — статистика конкретного користувача\n"
            "/revoke <id> — забрати доступ\n"
            "/approve <id> — дати доступ"
        )
    # ReplyKeyboardRemove прибирає стару системну клавіатуру знизу
    # екрана, яку Telegram міг закешувати від попередньої версії бота
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

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
            update, context, "Немає активних відстежень. Додай перше:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Додати товар", callback_data="menu:addwatch")],
                [InlineKeyboardButton("◀️ Меню", callback_data="menu:home")],
            ]),
        )
        return
    lines = ["📦 <b>Активні відстеження</b>\n", "Обери товар, щоб переглянути деталі:"]
    rows = []
    for w in watches:
        no_cat = "" if watch_category_ids(w) else " ⚠️ без категорії"
        lines.append(f"📌 <b>#{w['id']} {html.escape(w['label'])}</b>{no_cat}")
        rows.append(
            [
                InlineKeyboardButton(f"📊 Відкрити #{w['id']}", callback_data=f"watch_details:{w['id']}"),
                InlineKeyboardButton(f"✏️ Редагувати #{w['id']}", callback_data=f"editw:{w['id']}"),
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton("➕ Додати товар", callback_data="menu:addwatch")],
            [InlineKeyboardButton("🗑️ Видалити товар", callback_data="delwatch_prompt")],
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
    category_txt = html.escape(
        ", ".join(category_label(c["name"], with_original=True) for c in get_watch_categories(watch))
    ) or "усі (не обрано)"
    min_price = watch.get("min_price") or 0
    min_price_txt = f"{min_price:.0f}€" if min_price else "без обмеження"
    pct = watch["discount_threshold_pct"]
    lines = [
        f"📌 <b>#{watch['id']} {html.escape(watch['label'])}</b>",
        f"🎯 Бажаний прибуток: <b>{pct:g}%</b> від ціни продажу (мін. {MIN_PROFIT_EUR}€)",
        f"🗂️ Категорії: {category_txt}",
        f"💶 Мінімальна ціна: {min_price_txt}",
        (f"🧾 Обов'язкові характеристики: {html.escape(', '.join(get_required_aspects(watch)))}"
         if get_required_aspects(watch) else
         "💾 Лише лоти з відомою пам'яттю: "
         + ("так" if watch_requires_spec(watch) else "ні")
         + (" (авто)" if watch.get("require_spec") is None else "")),
    ]
    if not stats:
        lines.append("\n💰 <b>Ринок:</b>\nЩе не проаналізований (потрібно ≥"
                     f"{MIN_SAMPLE_SIZE} оголошень у групі).")
        return "\n".join(lines)

    lines.append("\n💰 <b>Купівля і продаж:</b>")
    for s in sorted(stats, key=lambda s: (s["cond_group"], s["spec_group"] != "*", s["spec_group"])):
        sale_price = s["sale_price"] or s["median_price"]
        buy_limit = max_buy_price(sale_price, pct)
        _, profit = estimate_resale_profit(sale_price, buy_limit)
        trend = ""
        if s["prev_median_price"]:
            if s["median_price"] < s["prev_median_price"] * 0.98:
                trend = " ⬇️"
            elif s["median_price"] > s["prev_median_price"] * 1.02:
                trend = " ⬆️"
        lines.append(
            f"\n• <b>{html.escape(_group_label(s['cond_group'], s['spec_group']))}</b> "
            f"(оголошень: {s['sample_size']})\n"
            f"  🛒 Купувати до: <b>{buy_limit:.0f}€</b>\n"
            f"  💶 Продати за: ~<b>{sale_price:.0f}€</b> ({html.escape(s['sale_source'] or 'оцінка')})\n"
            f"  💰 Прибуток при цьому: ~{profit:.0f}€\n"
            f"  📊 Медіана пропозицій: {s['median_price']:.0f}€{trend}"
        )
    lines.append(
        "\n<i>Ціна продажу — оцінка: поки бот не назбирав ≥"
        f"{MIN_SOLD_SAMPLE} «зниклих» (ймовірно проданих) лотів, це нижня чверть "
        "поточних пропозицій. Прибуток — після комісії eBay і доставки.</i>"
    )
    return "\n".join(lines)


async def _show_watch_details(update, context, watch):
    watch_id = watch["id"]
    rows = [
        [InlineKeyboardButton("🔎 Переглянути оголошення", callback_data=f"view_listings:{watch_id}")],
        [InlineKeyboardButton("🧩 Усі конфігурації", callback_data=f"configs:{watch_id}")],
        [InlineKeyboardButton("✏️ Редагувати", callback_data=f"editw:{watch_id}")],
        [InlineKeyboardButton("◀️ До активних відстежень", callback_data="menu:list")],
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
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
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
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
        return

    back_rows = [[InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")]]
    listings = get_current_listings(watch_id)
    if not listings:
        await show_panel(
            update, context,
            f"🧩 <b>#{watch_id} {html.escape(watch['label'])}: конфігурації</b>\n\n"
            "Ще немає даних з ринкового сканування. Натисни «🔄 Перерахувати медіану» "
            "на екрані товару й відкрий цей список знову.",
            reply_markup=InlineKeyboardMarkup(back_rows),
            parse_mode=ParseMode.HTML,
        )
        return

    groups = {}
    for row in listings:
        groups.setdefault((row["cond_group"], row["spec_group"]), []).append(row["price"])

    lines = [
        f"🧩 <b>#{watch_id} {html.escape(watch['label'])}: усі конфігурації</b>",
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
            f"від {min(prices):.0f}€, медіана {statistics.median(prices):.0f}€"
        )
    lines.append(
        f"\n<i>⚠️ — менше {MIN_SAMPLE_SIZE} оголошень: окремої оцінки «купити/продати» немає, "
        "лоти цієї конфігурації порівнюються із групою «усі конфігурації» свого стану.</i>\n"
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


@require_access
async def config_listings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """10 найдешевших поточних оголошень обраної групи (з останнього
    ринкового сканування — без додаткових запитів до eBay)."""
    query_cb = update.callback_query
    _, watch_id_str, idx_str = query_cb.data.split(":")
    watch_id = int(watch_id_str)
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
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
    top = rows_db[:10]

    spec_txt = "усі конфігурації" if spec == "*" else ("без конфігурації" if spec == "unspecified" else spec)
    header = (f"🔎 <b>#{watch_id} {html.escape(watch['label'])}</b>\n"
              f"{html.escape(CONDITION_LABELS.get(cond, cond).capitalize())} · {html.escape(spec_txt)} — "
              f"найдешевші {len(top)} з {len(rows_db)}")
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

    lines = [header]
    buttons = []
    for i, r in enumerate(top, 1):
        lines.append(f"\n<b>{i}.</b> {html.escape((r['title'] or 'без назви')[:120])}\n💶 {r['price']:.0f}€")
        buttons.append([InlineKeyboardButton(f"🔗 Відкрити #{i} · {r['price']:.0f}€", url=r["url"])])
    await show_panel(
        update, context, "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons + nav), parse_mode=ParseMode.HTML,
    )


async def _render_aspect_picker(update, context, watch):
    watch_id = watch["id"]
    options = context.user_data.get(f"asp_options_{watch_id}") or []
    selected = context.user_data.get(f"asp_selected_{watch_id}") or set()
    current = ", ".join(get_required_aspects(watch)) or (
        "авто" if watch.get("require_spec") is None else "без вимоги")
    back_row = [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")]
    await show_panel(
        update, context,
        f"🧾 <b>#{watch_id} {html.escape(watch['label'])}: обов'язкові характеристики</b>\n"
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
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
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
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
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
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
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
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
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
        f"🗂️ <b>#{watch_id} {html.escape(watch['label'])}: категорії</b>\n\n" + "Познач одну або кілька категорій, де продають сам товар (напр. консолі, а не ігри), "
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
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
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


@require_access
async def view_listings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    chat_id = update.effective_chat.id
    watch = get_watch(watch_id, chat_id)
    if watch is None:
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
        return

    try:
        # sort=price — eBay повертає лоти від найдешевших. Беремо із запасом
        # (частину відсіє перевірка назви) і показуємо 10 найдешевших.
        def _cheapest():
            found = search_in_categories(
                watch_category_ids(watch), limit=50, fresh=True, sort="price", **_watch_search_kwargs(watch),
            )
            _annotate_items(found, max_lookups=MAX_SPEC_LOOKUPS_PER_DEAL_SCAN, watch=watch)
            return _apply_item_filters(watch, found)

        items = await asyncio.to_thread(_cheapest)
    except Exception as e:
        log.exception("Не вдалося завантажити оголошення для watch #%s: %s", watch_id, e)
        await query_cb.answer("Не вдалося завантажити оголошення. Спробуй ще раз.", show_alert=True)
        return

    items.sort(key=lambda item: item["total_price"])
    items = items[:10]
    nav_rows = [
        [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")],
        [InlineKeyboardButton("📦 До відстежень", callback_data="menu:list")],
        [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
    ]

    if not items:
        await show_panel(
            update,
            context,
            f"🔎 <b>Оголошення для #{watch_id} {html.escape(watch['label'])}</b>\n\n"
            "Підходящих оголошень не знайдено.",
            reply_markup=InlineKeyboardMarkup(nav_rows),
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [
        f"🔎 <b>Оголошення для #{watch_id} {html.escape(watch['label'])}</b>",
        f"Найдешевші відповідні лоти: <b>{len(items)}</b>\n"
        f"🕒 Оновлено: {datetime.now().strftime('%H:%M:%S')}\n",
    ]
    rows = []
    for index, item in enumerate(items, 1):
        title = html.escape(item["title"][:160])
        lines.append(
            f"<b>{index}. {title}</b>\n"
            f"💶 {item['total_price']:.0f} {item['currency']} · "
            f"стан: {html.escape(item.get('condition') or 'н/д')}"
        )
        if item.get("url"):
            rows.append([InlineKeyboardButton(f"🔗 Відкрити оголошення #{index}", url=item["url"])])

    rows.extend(nav_rows)
    await show_panel(
        update,
        context,
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


EDIT_VALUE = 0  # окрема коротка розмова: введення нового значення


@require_access
async def edit_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«✏️ Редагувати» — що саме змінити в товарі."""
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
        return ConversationHandler.END
    context.user_data.pop("edit", None)
    min_price = watch.get("min_price") or 0
    rows = [
        [InlineKeyboardButton("🗂️ Змінити категорії", callback_data=f"chcat:{watch_id}")],
        [InlineKeyboardButton(f"🎯 Бажаний прибуток ({watch['discount_threshold_pct']:g}%)", callback_data=f"edpct:{watch_id}")],
        [InlineKeyboardButton(
            f"💶 Мінімальна ціна ({f'{min_price:.0f}€' if min_price else 'без обмеження'})",
            callback_data=f"edmin:{watch_id}")],
        [InlineKeyboardButton("🧾 Обов'язкові характеристики", callback_data=f"reqasp:{watch_id}")],
        [InlineKeyboardButton("🔄 Перерахувати медіану", callback_data=f"recalc_median:{watch_id}")],
        [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")],
    ]
    await show_panel(
        update, context,
        f"✏️ <b>#{watch_id} {html.escape(watch['label'])}</b>\n\nЩо хочеш відредагувати?\n\n"
        "<i>🔄 Перерахувати медіану — оновити ринкові ціни зараз, не чекаючи "
        "автоматичного оновлення раз на годину.</i>",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


@require_access
async def edit_value_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """edpct:<id> — бажаний прибуток, edmin:<id> — мінімальна ціна."""
    query_cb = update.callback_query
    kind, watch_id_str = query_cb.data.split(":")
    watch_id = int(watch_id_str)
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
        return ConversationHandler.END
    field = "pct" if kind == "edpct" else "min"
    context.user_data["edit"] = {"field": field, "watch_id": watch_id}
    back = [InlineKeyboardButton("◀️ Назад", callback_data=f"editw:{watch_id}")]

    if field == "pct":
        presets = [10, 15, 20, 25, 30]
        rows = [[InlineKeyboardButton(f"{p}%", callback_data=f"edval:{p}") for p in presets], back]
        text = (
            f"🎯 <b>#{watch_id}: бажаний прибуток</b>\nЗараз: {watch['discount_threshold_pct']:g}%\n\n"
            "Скільки відсотків від ціни продажу має лишатись тобі після комісії eBay і доставки "
            f"(мінімум {MIN_PROFIT_EUR}€). Чим більше — тим менше, але вигідніших знахідок.\n\n"
            f"Обери кнопкою або надішли число від {MIN_ALLOWED_THRESHOLD_PCT} до {MAX_ALLOWED_THRESHOLD_PCT}."
        )
    else:
        stats = get_market_stats(watch_id)
        medians = sorted(s["median_price"] for s in stats)
        presets = []
        if medians:
            base = medians[0]
            presets = sorted({max(5, round(base * pct / 100 / 5) * 5) for pct in (30, 40, 50)})
        rows = []
        if presets:
            rows.append([InlineKeyboardButton(f"{p:.0f}€", callback_data=f"edval:{p}") for p in presets])
        rows.append([InlineKeyboardButton("Без обмеження", callback_data="edval:0")])
        rows.append(back)
        current = watch.get("min_price") or 0
        text = (
            f"💶 <b>#{watch_id}: мінімальна ціна</b>\nЗараз: {f'{current:.0f}€' if current else 'без обмеження'}\n\n"
            "Лоти дешевші за цю ціну не враховуються — так відсіюються аксесуари й запчастини."
            + ("\nКнопки — 30/40/50% від найменшої медіани ринку." if presets else "")
            + "\n\nОбери кнопкою або надішли число в євро (0 — без обмеження)."
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

    if edit["field"] == "pct":
        if not (MIN_ALLOWED_THRESHOLD_PCT <= value <= MAX_ALLOWED_THRESHOLD_PCT):
            await show_panel(
                update, context,
                f"Значення має бути від {MIN_ALLOWED_THRESHOLD_PCT} до {MAX_ALLOWED_THRESHOLD_PCT}. Спробуй ще раз.",
                reply_markup=back,
            )
            return EDIT_VALUE
        update_threshold(watch_id, chat_id, value)
    else:
        if value < 0:
            await show_panel(update, context, "Ціна не може бути відʼємною. Спробуй ще раз.", reply_markup=back)
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
    else:
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
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
        return

    try:
        items, medians, _ = await _recalculate_watch_medians(watch, replace_existing=True)
    except Exception as e:
        log.exception("Не вдалося перерахувати медіану для watch #%s: %s", watch_id, e)
        await _notify_median_error(context.application, watch, e)
        await query_cb.answer("Не вдалося перерахувати медіану. Спробуй ще раз.", show_alert=True)
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
            f"Знайдено {len(items)} оголошень, але після фільтрації недостатньо "
            f"даних для медіани. Потрібно щонайменше {minimum} оголошення в одній "
            "групі стану та конфігурації.",
        )

    await _show_watch_details(update, context, get_watch(watch_id, chat_id))


@require_access
async def delwatch_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wid = int(update.callback_query.data.split(":")[1])
    chat_id = update.effective_chat.id
    remove_watch(wid, chat_id)
    await _ack_callback(update)
    await cmd_list(update, context)


@require_access
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Формат: /remove <id>")
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом, дивись /list")
        return
    if not get_watch(wid, chat_id):
        await update.message.reply_text("Такого відстеження немає, дивись /list")
        return
    remove_watch(wid, chat_id)
    await update.message.reply_text(f"🗑️ Відстеження #{wid} вимкнено.")


@require_access
async def cmd_setthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /setthreshold <id> <%>")
        return
    try:
        wid = int(context.args[0])
        pct = float(context.args[1].replace("%", ""))
    except ValueError:
        await update.message.reply_text("Некоректні значення. Формат: /setthreshold <id> <%>")
        return
    if not (MIN_ALLOWED_THRESHOLD_PCT <= pct <= MAX_ALLOWED_THRESHOLD_PCT):
        await update.message.reply_text(
            f"Значення має бути від {MIN_ALLOWED_THRESHOLD_PCT} до {MAX_ALLOWED_THRESHOLD_PCT}."
        )
        return
    if not get_watch(wid, chat_id):
        await update.message.reply_text("Такого відстеження немає, дивись /list")
        return
    update_threshold(wid, chat_id, pct)
    await update.message.reply_text(f"🎯 Бажаний прибуток для #{wid}: {pct}% від ціни продажу.")


@require_access
async def cmd_setminprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /setminprice <id> <€>\nПриклад: /setminprice 3 120 (0 — без обмеження)")
        return
    try:
        wid = int(context.args[0])
        value = float(context.args[1].replace("€", "").replace(",", "."))
    except ValueError:
        await update.message.reply_text("Некоректні значення. Формат: /setminprice <id> <€>")
        return
    if value < 0:
        await update.message.reply_text("Ціна не може бути відʼємною.")
        return
    if not get_watch(wid, chat_id):
        await update.message.reply_text("Такого відстеження немає, дивись /list")
        return
    update_watch_min_price(wid, chat_id, value)
    reset_watch_market(wid)  # інша вибірка — ринок аналізуємо заново
    shown = f"{value:.0f}€" if value else "без обмеження"
    await update.message.reply_text(f"💶 Мінімальна ціна для #{wid}: {shown}. Медіану буде перераховано.")


@require_access
async def cmd_requirespec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    values = {"on": 1, "off": 0, "auto": None}
    if len(context.args) != 2 or context.args[1].lower() not in values:
        await update.message.reply_text(
            "Формат: /requirespec <id> <on|off|auto>\n"
            "on — враховувати лише лоти з відомою пам'яттю (решта вважається аксесуарами)\n"
            "off — враховувати всі лоти\n"
            "auto — увімкнено для телефонів, ноутбуків і консолей"
        )
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом, дивись /list")
        return
    watch = get_watch(wid, chat_id)
    if not watch:
        await update.message.reply_text("Такого відстеження немає, дивись /list")
        return
    mode = context.args[1].lower()
    update_watch_require_spec(wid, chat_id, values[mode])
    reset_watch_market(wid)
    effective = watch_requires_spec(get_watch(wid, chat_id))
    await update.message.reply_text(
        f"💾 #{wid}: лише лоти з відомою пам'яттю — {'так' if effective else 'ні'}"
        f"{' (авто)' if mode == 'auto' else ''}. Ринок буде проаналізовано заново."
    )


@require_access
async def cmd_setexclude(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) < 1:
        await update.message.reply_text(
            "Формат: /setexclude <id> слово1 слово2 ...\n"
            "Приклад: /setexclude 3 broken defekt teile parts kaputt\n"
            "Щоб очистити список — /setexclude <id> без слів."
        )
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом, дивись /list")
        return

    if not get_watch(wid, chat_id):
        await update.message.reply_text("Такого відстеження немає, дивись /list")
        return

    exclude_text = " ".join(context.args[1:])
    update_watch_exclude(wid, chat_id, exclude_text)
    reset_watch_market(wid)
    shown = exclude_text if exclude_text else "(порожньо)"
    await update.message.reply_text(f"🚫 Виключені слова для #{wid} оновлено: {shown}")


@require_access
async def cmd_setconditions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) != 2 or context.args[1].lower() not in CONDITION_PRESETS:
        await update.message.reply_text(
            "Формат: /setconditions <id> <new|used|both>\n"
            "new — лише новий товар, used — лише вживаний, "
            "both — обидва (за замовчуванням)."
        )
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом, дивись /list")
        return

    if not get_watch(wid, chat_id):
        await update.message.reply_text("Такого відстеження немає, дивись /list")
        return

    preset = context.args[1].lower()
    update_watch_conditions(wid, chat_id, CONDITION_PRESETS[preset])
    reset_watch_market(wid)
    await update.message.reply_text(f"🏷️ Стан товару для #{wid} змінено на «{preset}».")


@require_access
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    stats = get_deal_stats(chat_id)
    if not stats:
        await show_panel(update, context, "Поки що немає жодної знахідки.", reply_markup=back_to_menu_keyboard())
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
        await query_cb.message.reply_text("⛔ Ця знахідка тобі не належить.")
        return

    status = "bought" if action == "buy" else "skipped"
    set_deal_status(deal_id, status)

    label = "✅ Куплено" if status == "bought" else "❌ Пропущено"
    await query_cb.edit_message_text(f"{query_cb.message.text}\n\n{label}")