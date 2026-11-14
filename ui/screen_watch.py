"""
Картка товару: /start, /menu, картка, конфігурації, мін. прибуток товару, вивчені слова, оновлення цін, видалення.
«🗂 Категорії товарів» — ui/screen_categories.py, «✏️ Редагувати» — ui/screen_edit.py.
"""

import asyncio
import html
import statistics

from analysis.laptops import scoped_label, spec_matches
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from core.settings import (
    UNDO_KEEP_HOURS,
    MIN_SAMPLE_SIZE,
    MIN_SOLD_SAMPLE,
    SEARCH_RESERVE,
    SOLD_LOOKBACK_DAYS,
    is_owner,
    log,
)
from analysis.textparse import (
    aspects_label,
    CONDITION_LABELS,
    _group_label,
    category_label,
    plural,
)
from analysis.learning import unlearn_word
from ui.undo import record as undo_record, short
from core.db import (
    get_current_listings,
    get_auto_min_price,
    get_learned_words,
    get_market_stats,
    min_profit_rule,
    profit_needed,
    profit_rule_label,
    set_watch_min_profit,
    get_price_history,
    get_required_aspects,
    watch_obs_summary,
    get_user_row,
    get_watch,
    get_watch_categories,
    list_watches,
    remove_watch,
    upsert_user_request,
)
from ebay.ebay_api import (
    browse_budget_left,
    fetch_browse_rate_limit,
)
from analysis.market import (
    watch_group,
    _recalculate_watch_medians,
    max_buy_price,
    minimum_sample_size_for_query,
    watch_requires_spec,
)
from ui.panel import (
    _ack_callback,
    menu_parts,
    show_main_menu,
    show_panel,
)
from ui.access import _notify_owner_new_request, require_access
from ui.notifications import _notify_median_error, _notify_median_problem
from analysis.sales import weekly_change
from ui.style import grid, hint, menu_row, title


def profit_source_text(watch, source):
    """«(свій для цього товару)» / «(категорії 🎮 Консолі)» / «(загальний)»."""
    if source == "own":
        return "свій для цього товару"
    if source == "category":
        from ui.screen_folders import folder_names
        return f"категорії {folder_names(watch['chat_id']).get(watch_group(watch), '')}"
    return "загальний"


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

    context.user_data["panel_force_new"] = True
    await show_main_menu(update, context)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показати головне меню знову (напр. якщо панель загубилась вище в чаті)."""
    context.user_data["panel_force_new"] = True
    await show_main_menu(update, context)


def _watch_details_text(watch):
    stats = get_market_stats(watch["id"])
    rule, source = min_profit_rule(watch)
    rule_txt = profit_rule_label(rule) + (" від ціни продажу" if rule[0] == "pct" else "")
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
        f"<i>🔎 пошук на eBay: «{html.escape(watch.get('query') or '')}»</i>",
        "",
        "⚙️ <b>Пошук</b>",
        f"🎯 Вигідно, якщо чистий прибуток ≥ <b>{rule_txt}</b> ({html.escape(profit_source_text(watch, source))})"
        " — після доставки покупцю",
        f"🗂️ Категорії eBay: {category_txt}",
        f"💶 Мінімальна ціна: {min_price_txt}",
        (f"🧾 Обов'язкові характеристики: {html.escape(aspects_label(get_required_aspects(watch)), quote=False)}"
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
        lines += ["", "👁 <b>Спостереження</b>",
                  f"📋 Відстежую оголошень зараз: <b>{obs['active']}</b>", sold_line]
        if obs["pending"]:
            lines.append(f"⏳ У черзі на перевірку «продано?»: <b>{obs['pending']}</b>")
        if obs["withdrawn"]:
            lines.append(f"🚫 Зникли без продажу (не враховуються): {obs['withdrawn']}")
    learned = _learned_excluded(watch["id"])
    if learned:
        lines.append(f"🧠 Відсіюю за вивченими словами: {html.escape(', '.join(learned))}")
    if not stats:
        lines.append("\n⏳ <b>Ціни ще не пораховані</b> — потрібно щонайменше "
                     f"{plural(MIN_SAMPLE_SIZE, 'оголошення', 'оголошення', 'оголошень')} одного типу продавця (🏪 магазин чи 👤 приватні).")
        return "\n".join(lines)

    lines.append("\n💰 <b>Купівля і продаж</b>")
    history = get_price_history(watch["id"])
    for s in sorted(stats, key=lambda s: (s["cond_group"], s["spec_group"] != "*", s["spec_group"])):
        sale_price = s["sale_price"] or s["median_price"]
        buy_limit = max_buy_price(sale_price, profit_needed(rule, sale_price))
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
    lines.append("\n" + hint(
        "«Продати за» — медіана реальних продажів; поки їх менше "
        f"{MIN_SOLD_SAMPLE} — оцінка за найдешевшою чвертю поточних оголошень.\n"
        f"«Купувати до» = продати − 7€ доставки − мін. прибуток ({rule_txt}).\n"
        "📊 Типова ціна оголошень — за скільки товар зараз виставляють (для довідки й тренду).",
        "ℹ️ Як рахуються ціни"))
    return "\n".join(lines)


def _learned_excluded(watch_id):
    return sorted(w for w, st in get_learned_words(watch_id).items() if st == "excluded")


def _count_unrecognized(watch):
    from analysis.unrecognized import count_unrecognized
    try:
        return count_unrecognized(watch)
    except Exception as e:   # лічильник не має ламати екран товару
        log.debug("Не вдалося порахувати нерозпізнані: %s", e)
        return 0


async def _show_watch_details(update, context, watch, note=""):
    watch_id = watch["id"]
    learned = _learned_excluded(watch_id)
    unknown = await asyncio.to_thread(_count_unrecognized, watch)
    group = await asyncio.to_thread(watch_group, watch)
    rows = [
        [InlineKeyboardButton("📋 Оголошення", callback_data=f"view_listings:{watch_id}"),
         InlineKeyboardButton("🔍 Перевірити оголошення", callback_data=f"chkl:{watch_id}")],
        [InlineKeyboardButton("🧩 Конфігурації", callback_data=f"configs:{watch_id}"),
         InlineKeyboardButton("📈 Продажі", callback_data=f"sales:{watch_id}")],
        [InlineKeyboardButton(_profit_button(watch), callback_data=f"wmp:{watch_id}")],
    ]
    extra = [InlineKeyboardButton("🔄 Оновити ціни", callback_data=f"recalc_median:{watch_id}")]
    if unknown:   # «🛠 Нерозпізнані» — лише коли такі оголошення є
        extra.append(InlineKeyboardButton(f"🛠 Нерозпізнані ({unknown})", callback_data=f"unk:{watch_id}:0:w"))
    if learned:   # кнопка лише коли є що прибирати
        extra.append(InlineKeyboardButton(f"🧠 Вивчені слова ({len(learned)})", callback_data=f"lwords:{watch_id}"))
    rows += grid(extra)
    rows += [
        [
            InlineKeyboardButton("✏️ Редагувати", callback_data=f"editw:{watch_id}"),
            InlineKeyboardButton("🗑️ Видалити", callback_data=f"delwatch_ask:{watch_id}"),
        ],
        [InlineKeyboardButton("◀️ Категорія", callback_data=f"wgrp:{group}"), *menu_row()],
    ]
    await show_panel(
        update,
        context,
        (f"{note}\n\n" if note else "") + _watch_details_text(watch),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


WATCH_MIN_PROFIT_CHOICES = (10, 15, 20, 25, 30, 40, 50, 60, 80, 100, 150, 200)
_SOURCE_SHORT = {"own": "", "category": " (категорія)", "general": " (загальний)"}


def _profit_button(watch):
    rule, source = min_profit_rule(watch)
    return f"💰 Мін. прибуток: {profit_rule_label(rule)}{_SOURCE_SHORT[source]}"


@require_access
async def watch_min_profit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """wmp:<товар> — вибір мін. прибутку товару; wmp:<товар>:<сума> — зберегти; wmp:<товар>:def — як загальний."""
    await _ack_callback(update)
    parts = update.callback_query.data.split(":")
    watch = get_watch(int(parts[1]), update.effective_chat.id)
    if watch is None:
        return
    note = ""
    if len(parts) > 2:
        value = None if parts[2] == "def" else float(parts[2])
        await asyncio.to_thread(set_watch_min_profit, watch, value)
        watch = get_watch(watch["id"], update.effective_chat.id)
        note = ("✅ Тепер як категорія / загальний мін. прибуток." if value is None
                else f"✅ Мін. прибуток для «{html.escape(watch['label'])}»: <b>{value:.0f}€</b>.")
        return await _show_watch_details(update, context, watch, note=note)
    rule, source = min_profit_rule(watch)
    own = source == "own"
    inherited, inherited_src = min_profit_rule({**watch, "min_profit": None})   # без свого значення
    inherited_txt = f"{profit_rule_label(inherited)}, {profit_source_text(watch, inherited_src)}"
    buttons = [InlineKeyboardButton(("✅ " if own and v == rule[1] else "") + f"{v}€",
                                    callback_data=f"wmp:{watch['id']}:{v}") for v in WATCH_MIN_PROFIT_CHOICES]
    rows = grid(buttons, 4)
    rows.append([InlineKeyboardButton(("✅ " if not own else "") + f"↩️ Як категорія / загальний ({inherited_txt})",
                                      callback_data=f"wmp:{watch['id']}:def")])
    rows.append([InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch['id']}")])
    text = (title("💰", f"Мін. прибуток · {html.escape(watch['label'])}")
            + f"\nЗараз: <b>{profit_rule_label(rule)}</b> ({html.escape(profit_source_text(watch, source))})\n\n"
            "Бот пропонує оголошення цього товару, лише якщо після перепродажу (мінус доставка покупцю) "
            "лишається щонайменше стільки. Від цього залежить і «купувати до».\n\n"
            + hint("Свій поріг товару — найголовніший: він важливіший за поріг категорії й загальний.\n"
                   f"«↩️ Як категорія / загальний» — повернути спільне значення (зараз {html.escape(inherited_txt)}).\n"
                   "Пороги для цілих категорій (у € чи %) — у «⚙️ Налаштування» → «💰 Мін. прибуток»."))
    await show_panel(update, context, text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)


async def _show_learned_words(update, context, watch, note=""):
    words = _learned_excluded(watch["id"])
    lines = [note] if note else []
    lines.append(f"🧠 <b>Вивчені слова — {html.escape(watch['label'])}</b>")
    lines.append("Бот сам вивів ці слова з оголошень, які ти відхилив (❌ Не той товар), і "
                 "відсіює оголошення з ними. Натисни слово, щоб більше не відсіювати — "
                 "бот його більше не пропонуватиме.")
    rows = [[InlineKeyboardButton(f"🗑 {w}", callback_data=f"lwdel:{watch['id']}:{i}")]
            for i, w in enumerate(words)]
    rows = [sum(rows[i:i + 2], []) for i in range(0, len(rows), 2)]   # по два в ряд
    rows.append([InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch['id']}")])
    await show_panel(update, context, "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
                     parse_mode=ParseMode.HTML)


@require_access
async def learned_words_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """lwords:<watch_id> — список вивчених слів; lwdel:<watch_id>:<номер> — прибрати слово."""
    query_cb = update.callback_query
    parts = query_cb.data.split(":")
    watch = get_watch(int(parts[1]), update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    note = ""
    if parts[0] == "lwdel":
        words = _learned_excluded(watch["id"])
        idx = int(parts[2])
        if idx >= len(words):
            await query_cb.answer("Список змінився — онови екран.", show_alert=True)
        else:
            word = words[idx]
            await asyncio.to_thread(unlearn_word, watch["id"], update.effective_chat.id, word)
            undo_record(context, update.effective_chat.id, "word_unlearn", f"слово «{word}»",
                        watch_id=watch["id"], word=word, screen="learned")
            await query_cb.answer(f"🗑 «{word}» більше не відсіюється")
            note = f"✅ «{html.escape(word)}» прибрано — такі оголошення знову враховуються."
            watch = get_watch(watch["id"], update.effective_chat.id)
            if not _learned_excluded(watch["id"]):   # слів не лишилось — назад до картки
                return await _show_watch_details(update, context, watch)
    else:
        await _ack_callback(update)
    await _show_learned_words(update, context, watch, note=note)


@require_access
async def watch_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    await _show_watch_details(update, context, watch)


MAX_CONFIG_BUTTONS = 25


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

    # Основні групи — ті самі, що в «💰 Купівля і продаж» на екрані товару (з ціною, ≥ MIN_SAMPLE_SIZE
    # оголошень; разом із ширшими класами); решта точних конфігурацій — у «📂 Інші конфігурації»
    exact: dict = {}
    for row in listings:
        exact.setdefault((row["cond_group"], row["spec_group"]), []).append(row["price"])
    market_keys = [(m["cond_group"], m["spec_group"]) for m in sorted(
        get_market_stats(watch_id), key=lambda m: (m["cond_group"], m["spec_group"] != "*", m["spec_group"]))]
    main = []
    for cond, spec in market_keys:
        prices = [r["price"] for r in listings if r["cond_group"] == cond and spec_matches(r["spec_group"], spec)]
        if prices:
            main.append(((cond, spec), prices))
    main_set = {key for key, _ in main}
    others = sorted(((k, p) for k, p in exact.items() if k not in main_set),
                    key=lambda kv: (kv[0][0], -len(kv[1]), statistics.median(kv[1])))
    show_small = query_cb.data.endswith(":other")
    shown = others if show_small else main

    def spec_label(spec, short=False):
        if spec == "*":
            return "усі" if short else "усі конфігурації"
        if spec == "unspecified":
            return "без конфігурації" if short else "конфігурація не вказана"
        # Ширша група з ціною («RTX 5050») — з поясненням «· усі процесори»; точні з «Інших» — як є
        return spec if show_small else scoped_label(spec)

    title = "інші конфігурації" if show_small else "конфігурації з ціною"
    lines = [
        f"🧩 <b>{html.escape(watch['label'])}: {title}</b>",
        f"Оголошень в останньому скануванні: <b>{len(listings)}</b>",
    ]
    if show_small:
        lines.append(f"<i>Тут менше {MIN_SAMPLE_SIZE} оголошень (або невідома відеокарта): окремої ціни немає, "
                     "такі оголошення порівнюються з ширшою групою свого стану.</i>")
    else:
        lines.append("<i>Ті самі групи, що в «💰 Купівля і продаж» на екрані товару.</i>")
    current_cond = None
    for (cond, spec), prices in shown:
        if cond != current_cond:
            current_cond = cond
            lines.append(f"\n<b>{html.escape(CONDITION_LABELS.get(cond, cond).capitalize())}</b>")
        lines.append(
            f"• <b>{html.escape(spec_label(spec))}</b> — {len(prices)} огол., "
            f"від {min(prices):.0f}€, типова {statistics.median(prices):.0f}€"
        )
    if not shown:
        lines.append("\nТут поки порожньо." if show_small else
                     f"\nГруп з ціною ще немає — у жодній конфігурації поки немає {MIN_SAMPLE_SIZE} оголошень.")
    if not show_small and others:
        lines.append(f"\n📂 Ще {plural(len(others), 'конфігурація', 'конфігурації', 'конфігурацій')} "
                     f"без окремої ціни — кнопка «📂 Інші конфігурації».")
    lines.append("\nНатисни групу нижче, щоб побачити її найдешевші оголошення.")

    choices = []
    for (cond, spec), prices in shown:
        cond_txt = CONDITION_LABELS.get(cond, cond).capitalize()
        label = f"🔎 {cond_txt} — усі ({len(prices)})" if spec == "*" else \
            f"🔎 {cond_txt} · {spec_label(spec, short=True)} ({len(prices)})"
        choices.append(((cond, spec), label))
    choices = choices[:MAX_CONFIG_BUTTONS]
    context.user_data[f"cfg_groups_{watch_id}"] = [key for key, _ in choices]
    rows = [[InlineKeyboardButton(label[:60], callback_data=f"cfgl:{watch_id}:{i}")]
            for i, (_, label) in enumerate(choices)]
    if show_small:
        back_rows = [[InlineKeyboardButton("◀️ До конфігурацій", callback_data=f"configs:{watch_id}")]] + back_rows
    elif others:
        rows.append([InlineKeyboardButton(f"📂 Інші конфігурації ({len(others)})",
                                          callback_data=f"configs:{watch_id}:other")])
    await show_panel(
        update, context, "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows + back_rows),
        parse_mode=ParseMode.HTML,
    )


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
            f"{plural(minimum, 'оголошення', 'оголошення', 'оголошень')} одного типу продавця (🏪 магазин чи 👤 приватні) й конфігурації.",
        )

    await _show_watch_details(update, context, get_watch(watch_id, chat_id))


def _few_listings_note(watch, items):
    """«📉 Замало оголошень: rog strix g15 (8: вживані 5, нові 3 — треба 8 одного стану)»."""
    minimum = minimum_sample_size_for_query(watch["query"])
    counts: dict = {}
    for it in items or []:
        counts[it["cond_group"]] = counts.get(it["cond_group"], 0) + 1
    parts = ", ".join(f"{CONDITION_LABELS.get(c, c)} {n}" for c, n in sorted(counts.items(), key=lambda x: -x[1]))
    found = f"{len(items or [])}: {parts}" if parts else "0"
    return (f"📉 Замало оголошень: {html.escape(watch['label'])} "
            f"({found} — треба {minimum} в одній групі)")


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
    context.bot_data["refresh_all_running"] = True
    context.bot_data["refresh_all_cancel"] = False
    # Окремим завданням — щоб бот міг обробити «⏹ Зупинити», поки йде оновлення
    context.bot_data["refresh_all_task"] = asyncio.create_task(_refresh_all(update, context))


REFRESH_STOP_KB = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Зупинити", callback_data="refresh_stop")]])


async def refresh_stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """refresh_stop — зупинити «💰 Оновити ціни» після поточного товару."""
    if not is_owner(update.effective_user.id):
        await _ack_callback(update)
        return
    if not context.bot_data.get("refresh_all_running"):
        await _ack_callback(update)
        await show_main_menu(update, context)
        return
    context.bot_data["refresh_all_cancel"] = True
    try:
        await update.callback_query.answer("⏹ Зупиняю — допрацюю поточний товар і покажу підсумок.")
    except Exception:
        pass


async def _refresh_all(update, context):
    chat_id = update.effective_chat.id
    watches = list_watches(chat_id=chat_id, active_only=True)
    done: list = []
    failed: list = []
    few: list = []      # не помилка: оголошень замало, щоб порахувати ціну
    skipped: list = []
    stopped: list = []
    try:
        for n, watch in enumerate(watches, 1):
            if context.bot_data.get("refresh_all_cancel"):
                stopped = [w["label"] for w in watches[n - 1:]]
                break
            if browse_budget_left() < SEARCH_RESERVE:
                skipped = [w["label"] for w in watches[n - 1:]]
                break
            await show_panel(
                update, context,
                f"🔄 Оновлюю ціни з eBay… ({n}/{len(watches)})\n\n⏳ <b>{html.escape(watch['label'])}</b>",
                reply_markup=REFRESH_STOP_KB, parse_mode=ParseMode.HTML,
            )
            try:
                items, stats, _ = await _recalculate_watch_medians(watch, replace_existing=True)
                if stats:
                    done.append(watch["label"])
                else:
                    few.append(_few_listings_note(watch, items))
            except Exception as e:
                log.warning("Не вдалося оновити ціни для watch #%s: %s", watch["id"], e)
                failed.append(watch["label"])
        try:
            await asyncio.to_thread(fetch_browse_rate_limit)
        except Exception as e:
            log.warning("Не вдалося оновити дані про ліміт eBay: %s", e)
    except Exception as e:
        log.exception("Оновлення цін усіх товарів перервалось: %s", e)
    finally:
        context.bot_data["refresh_all_running"] = False
        context.bot_data["refresh_all_cancel"] = False

    notes = []
    if done:
        notes.append(f"✅ Ціни оновлено: {plural(len(done), 'товар', 'товари', 'товарів')}")
    notes += few
    if failed:
        notes.append("⚠️ Не вдалося порахувати: " + html.escape(", ".join(failed)))
    if skipped:
        notes.append("⏸ Замало запитів до eBay на сьогодні, не оновлено: " + html.escape(", ".join(skipped)))
    if stopped:
        notes.append("⏹ Зупинено, не оновлено: " + html.escape(", ".join(stopped)))
    if not watches:
        notes.append("Товарів ще немає — оновлено лише дані про запити.")
    menu_text, menu_kb = await menu_parts(update.effective_user.id)
    await show_panel(update, context, menu_text + "\n\n" + "\n".join(notes),
                     reply_markup=menu_kb, parse_mode=ParseMode.HTML)


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
        f"🗑️ Видалити товар «{html.escape(watch['label'])}»?\n\n↩️ Протягом {UNDO_KEEP_HOURS} год видалення можна скасувати — історія цін і продажів зберігається.",
        reply_markup=keyboard, parse_mode=ParseMode.HTML,
    )


@require_access
async def delwatch_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wid = int(update.callback_query.data.split(":")[1])
    chat_id = update.effective_chat.id
    watch = get_watch(wid, chat_id)
    remove_watch(wid, chat_id)
    if watch:
        undo_record(context, chat_id, "watch_delete", f"видалення «{short(watch['label'])}»",
                    watch_id=wid, screen="watch")
    await _ack_callback(update)
    await cmd_list(update, context)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats і «📊 Статистика» — див. screen_stats."""
    from ui.screen_stats import stats_callback   # тут, щоб не було циклу імпортів
    await stats_callback(update, context)


# Імпорти з інших екранів — унизу, щоб модулі могли посилатися один на одного
from ui.screen_categories import (  # noqa: E402
    cmd_list,
)
