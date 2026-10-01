"""
«📈 Продажі» товару: статистика продажів за конфігураціями, ❌ чужий товар, ⏳ перевірити зараз.
"""

import asyncio
import html
import statistics
import time
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from settings import SOLD_LOOKBACK_DAYS, LOCAL_TZ, is_owner
from textparse import CONDITION_LABELS, _search_tokens, plural
from learning import learned_words_note, reject_and_learn
from undo import record as undo_record, short
from laptops import spec_matches
from db import (
    get_obs_rows,
    delete_listing_obs_by_ids,
    get_current_listings,
    get_sold_listings,
    active_listing_counts,
    watch_obs_summary,
    get_watch,
)
from market import refresh_sale_prices, filter_outliers
from panel import _ack_callback, show_panel
from access import require_access
from ebay_user import is_connected
from trading_api import verify_watch_now
from sales import speed_text, summarize


SALES_LIST_MAX = 20   # скільки останніх продажів показувати в списку (з фільтром — лише потрібної конфігурації)


def _spec_label(spec):
    return "без конфігурації" if spec == "unspecified" else spec


def _sales_specs(sold):
    """Конфігурації, що траплялись у продажах, — від найчастішої."""
    counts = {}
    for r in sold:
        spec = r["spec_group"] or "unspecified"
        counts[spec] = counts.get(spec, 0) + 1
    return sorted(counts, key=lambda s: (-counts[s], s))


def _flt_spec(sold, flt):
    """flt «f<номер>» — конфігурація зі списку _sales_specs; інше — усі продажі."""
    if isinstance(flt, str) and flt.startswith("f") and flt[1:].isdigit():
        specs = _sales_specs(sold)
        idx = int(flt[1:])
        return specs[idx] if idx < len(specs) else None
    return None


def _filtered(sold, spec):
    return [r for r in sold if (r["spec_group"] or "unspecified") == spec] if spec else list(sold)


def _listed(sold, flt):
    spec = _flt_spec(sold, flt)
    return _filtered(sold, spec)[:SALES_LIST_MAX], spec


def _sales_text(watch, sold, show_account_hint=False, flt="", active=None):
    """Статистика продажів товару за конфігураціями (з таблиці спостережень, без запитів до eBay)."""
    title = f"📈 <b>{html.escape(watch['label'])}: продажі за {SOLD_LOOKBACK_DAYS} днів</b>"
    active = active or {}
    tracking = f"📋 Зараз у продажу (бот відстежує): <b>{sum(active.values())}</b> оголошень"
    if not sold:
        text = (f"{title}\n\n{tracking}\n\nПродажів ще не помічено. Бот вважає оголошення проданим, коли воно зникає "
                "з eBay задовго до кінця строку (або eBay підтверджує продаж). Статистика "
                "накопичується з кожним ринковим скануванням — зазирни через день-два.")
        return text + ("\n\n💡 Підключи «🔐 Акаунт eBay» в меню — тоді бот перевірятиме кожен продаж через eBay."
                       if show_account_hint else "")

    week_ago = time.time() - 7 * 86400
    confirmed = sum(1 for r in sold if r["sold_check"] == "sold")
    week = sum(1 for r in sold if r["gone_at"] >= week_ago)
    lines = [
        title, "",
        tracking,
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
            + (f"\n  📋 зараз у продажу: {active[(cond, spec)]}" if active.get((cond, spec)) else "")
        )

    shown, flt_spec = _listed(sold, flt)
    total = len(_filtered(sold, flt_spec))
    title_list = "\n<b>Останні продажі</b>" + (f" · {html.escape(_spec_label(flt_spec))}" if flt_spec else "")
    if total > len(shown):
        title_list += f" (показано {len(shown)} останніх з {total})"
    lines.append(title_list + ":")
    for n, r in enumerate(shown, 1):
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


def _sales_keyboard(watch_id, sold, flt="", extra_rows=(), pending=0):
    shown, flt_spec = _listed(sold, flt)
    key = flt if flt_spec else "a"
    buttons = [InlineKeyboardButton(f"❌ {n}", callback_data=f"srej:{watch_id}:{key}:{r['item_id']}")
               for n, r in enumerate(shown, 1)]
    rows = list(extra_rows)
    if pending:
        rows.append([InlineKeyboardButton(f"⏳ Перевірити зараз ({pending})", callback_data=f"schk:{watch_id}:{key}")])
    # Фільтр списку за конфігурацією — щоб не гортати, а одразу бачити, напр., лише 256GB
    specs = _sales_specs(sold)
    if len(specs) > 1:
        counts = {s: len(_filtered(sold, s)) for s in specs}
        filters = [InlineKeyboardButton(("• " if not flt_spec else "") + f"Усі ({len(sold)})",
                                        callback_data=f"sales:{watch_id}:a")]
        filters += [InlineKeyboardButton(("• " if s == flt_spec else "") + f"{_spec_label(s)[:24]} ({counts[s]})",
                                         callback_data=f"sales:{watch_id}:f{i}")
                    for i, s in enumerate(specs)]
        rows += [filters[i:i + 3] for i in range(0, len(filters), 3)]
    rows += [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")])
    return InlineKeyboardMarkup(rows)


async def _show_sales(update, context, watch, flt="", note="", extra_rows=()):
    """flt — фільтр списку: «a» / "" — усі продажі, «f<номер>» — одна конфігурація."""
    flt = flt if isinstance(flt, str) else ""
    sold = get_sold_listings(watch["id"])
    hint = is_owner(update.effective_user.id) and not is_connected()
    text = _sales_text(watch, sold, show_account_hint=hint, flt=flt,
                       active=active_listing_counts(watch["id"]))
    pending = watch_obs_summary(watch["id"])["pending"] if is_connected() else 0
    if pending:
        text += (f"\n\n⏳ Ще перевіряються через eBay: <b>{pending}</b> — у статистику потраплять, "
                 "лише коли eBay підтвердить продаж.")
    if note:
        text = f"{note}\n\n{text}"
    await show_panel(update, context, text,
                     reply_markup=_sales_keyboard(watch["id"], sold, flt, extra_rows, pending=pending),
                     parse_mode=ParseMode.HTML)


@require_access
async def sales_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """sales:<id>[:<фільтр>] — статистика продажів; фільтр «a» — усі, «f<номер>» — одна конфігурація."""
    query_cb = update.callback_query
    parts = query_cb.data.split(":")
    watch = get_watch(int(parts[1]), update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    await _ack_callback(update)
    await _show_sales(update, context, watch, parts[2] if len(parts) > 2 else "")


@require_access
async def sales_check_now_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """schk:<watch_id>:<фільтр> — перевірити через eBay зниклі оголошення товару прямо зараз."""
    query_cb = update.callback_query
    _, watch_id, flt = query_cb.data.split(":")
    watch = get_watch(int(watch_id), update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    await show_panel(update, context, f"⏳ Перевіряю через eBay зниклі оголошення «{html.escape(watch['label'])}»…")
    counts = await asyncio.to_thread(verify_watch_now, watch["id"])
    await asyncio.to_thread(refresh_sale_prices, watch["id"])
    if counts:
        labels = {"sold": "✅ продано", "unsold": "🚫 знято", "active": "↩️ ще продається",
                  "unknown": "❔ eBay не відповів", "not_found": "❔ не знайдено"}
        note = "Перевірено: " + ", ".join(f"{labels.get(k, k)} — {v}" for k, v in sorted(counts.items()))
    else:
        note = "Нічого не перевірено — вичерпано ліміт перевірок на сьогодні або немає входу в акаунт eBay."
    await _show_sales(update, context, watch, flt, note=html.escape(note))


@require_access
async def sales_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """srej:<watch_id>:<фільтр>:<item_id> — прибрати чужий лот зі статистики продажів."""
    query_cb = update.callback_query
    _, watch_id, flt, item_id = query_cb.data.split(":", 3)
    watch = get_watch(int(watch_id), update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Цей товар уже видалено.", show_alert=True)
        return
    row = next((r for r in get_sold_listings(watch["id"]) if r["item_id"] == item_id), None)
    if row is None:
        await query_cb.answer("Цього продажу вже немає в списку.")
        return await _show_sales(update, context, watch, flt)
    # «Не той товар»: більше не враховується ні в продажах, ні в цінах, ні в пошуку;
    # якщо в таких назвах повторюються слова — бот їх вивчить (як ❌ Інший товар)
    saved: dict = {}
    words = await asyncio.to_thread(reject_and_learn, watch, item_id, row["title"], saved)
    if words:
        # Продажі з щойно вивченими словами теж чужі — прибираємо й їх
        word_set = set(words)
        stale = [r["item_id"] for r in get_sold_listings(watch["id"])
                 if _search_tokens(r.get("title") or "") & word_set]
        saved.setdefault("obs", []).extend(get_obs_rows(watch["id"], stale))
        delete_listing_obs_by_ids(watch["id"], stale)
    undo_record(context, update.effective_chat.id, "reject", f"❌ «{short(row['title'], 28)}»",
                watch_id=watch["id"], item_id=item_id, screen="sales", page=flt, refresh_sales=True,
                **saved)
    await asyncio.to_thread(refresh_sale_prices, watch["id"])
    await query_cb.answer("❌ Прибрано — на ціни більше не впливає")
    note = f"❌ Прибрано: {html.escape((row['title'] or '')[:60])}"
    extra = []
    if words:
        note += "\n" + html.escape(learned_words_note(words))
        extra = [[InlineKeyboardButton(f"↩️ Не відсіювати «{w}»", callback_data=f"unlw:{watch['id']}:{w}")]
                 for w in words]
    await _show_sales(update, context, watch, flt, note=note, extra_rows=extra)


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
        if r["cond_group"] == cond and spec_matches(r["spec_group"], spec) and r.get("url")
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


# Імпорти з інших екранів — унизу, щоб модулі могли посилатися один на одного
from screen_common import (  # noqa: E402
    _ago,
)
from screen_listings import (  # noqa: E402
    _render_listing_panel,
)
