"""
Товари: /start, меню, «📦 Мої товари», картка товару, конфігурації, редагування (категорії, характеристики, мін. ціна), оновлення цін, видалення, статистика.
"""

import asyncio
import html
import statistics
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from settings import (
    MIN_PRICE_SUGGESTION_PCT,
    MIN_SAMPLE_SIZE,
    MIN_SOLD_SAMPLE,
    SEARCH_RESERVE,
    SOLD_LOOKBACK_DAYS,
    is_owner,
    log,
)
from textparse import (
    CONDITION_LABELS,
    _group_label,
    category_label,
    plural,
)
from db import (
    encode_required_aspects,
    get_current_listings,
    get_auto_min_price,
    get_learned_words,
    get_deal_stats,
    get_market_stats,
    get_min_profit,
    get_price_history,
    get_required_aspects,
    watch_obs_summary,
    get_user_row,
    get_watch,
    get_watch_categories,
    list_watches,
    remove_watch,
    mark_market_stale,
    reset_watch_market,
    update_watch_categories,
    update_watch_min_price,
    update_watch_required_aspect,
    upsert_user_request,
    watch_category_ids,
)
from ebay_api import (
    aspect_options_for_categories,
    browse_budget_left,
    fetch_browse_rate_limit,
    get_category_options,
)
from market import (
    _recalculate_watch_medians,
    apply_filters_to_history,
    max_buy_price,
    minimum_sample_size_for_query,
    watch_requires_spec,
)
from panel import (
    refresh_usage_callback,
    _ack_callback,
    back_to_menu_keyboard,
    build_main_menu,
    main_menu_text,
    show_main_menu,
    show_panel,
)
from access import _notify_owner_new_request, require_access
from notifications import _notify_median_error, _notify_median_problem
from sales import weekly_change


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
    old_ids = set(watch_category_ids(watch))
    if choice == "all":
        new_ids = set()
        update_watch_categories(watch_id, chat_id, [])
    elif choice == "done":
        if not selected:
            await query_cb.answer("Познач хоча б одну категорію або обери «Усі категорії».", show_alert=True)
            return
        new_ids = set(selected)
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
    # Категорії лише ДОДАНО (або «усі категорії» замість обмеження): старі оголошення й
    # продажі лишаються правдивими — зберігаємо історію, ринок просто перерахується з ширшою вибіркою
    only_added = (not new_ids) or (old_ids and old_ids <= new_ids)
    if new_ids == old_ids:
        pass
    elif only_added:
        mark_market_stale(watch_id)
    else:
        # Характеристики різних категорій різні — повертаємо автоматичний режим
        update_watch_required_aspect(watch_id, chat_id, None, None)
        # Категорію прибрано/замінено — в історії могли бути оголошення з неї; рахуємо ринок заново
        reset_watch_market(watch_id)
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


# Імпорти з інших екранів — унизу, щоб модулі могли посилатися один на одного
from screen_common import (  # noqa: E402
    ASPECT_CHOICE_TEXT,
    _aspect_keyboard,
    _category_keyboard,
)
from screen_discover import (  # noqa: E402
    discover_callback,
)
