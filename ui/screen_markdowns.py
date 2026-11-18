"""
«📉 Знизили ціну» і його налаштування «📉 Поріг знижки» (поріг знижки й які знижки показувати).
"""

import asyncio
import html
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from core.settings import MARKDOWN_HAGGLE_PCT, MARKDOWN_MODES, PRICE_DROP_CHOICES, PRICE_DROP_MIN_DAYS
from analysis.deal_reprice import basis_line, specs_line
from core.db import (
    dismiss_tracks,
    get_drop_pct,
    get_markdown_mode,
    get_watch,
    mark_track_seen,
    pickup_only_items,
    set_drop_pct,
    set_markdown_mode,
)
from analysis.learning import hide_item, learned_words_note, reject_and_learn
from analysis.laptops import laptop_warnings
from analysis.markdowns import markdown_list, recheck_rows
from ui.panel import _ack_callback, run_with_progress, show_panel
from ui.access import require_access
from analysis.textparse import PICKUP_NOTE, plural
from ui.undo import record as undo_record, short
from ui.style import hint, menu_row, note_line, title

PER_PAGE = 5
MODE_LABELS = {"deal": "✅ Лише вигідні", "haggle": f"🎯 Вигідні + торг до {MARKDOWN_HAGGLE_PCT}%", "all": "📋 Усі"}


def _card(n, r):
    new = "🆕 " if r["seen_at"] is None else ""
    listed = r["listed_at"] or r["first_seen"]
    days = int((time.time() - listed) // 86400)
    changed = ""
    if r["price_changed_at"]:
        ago = int((time.time() - r["price_changed_at"]) // 86400)
        changed = " · знижено " + ("сьогодні" if ago <= 0 else "вчора" if ago == 1
                                   else f"{plural(ago, 'день', 'дні', 'днів')} тому")
    lines = [
        f"<b>{n}. {new}{html.escape(r['watch_label'])}</b> · висить {plural(days, 'день', 'дні', 'днів')}",
        f"<i>{html.escape((r['title'] or '')[:90])}</i>",
        *([html.escape(sp)] if (sp := specs_line(r["item_id"], r["title"], r.get("spec_group"))) else []),
        f"📉 було {r['first_price']:.0f}€ → зараз <b>{r['price']:.0f}€</b> (−{r['drop_pct']:.0f}%){changed}",
    ]
    if r["price"] <= r["buy_limit"]:
        lines.append(f"✅ Вже вигідно: продати ~{r['sale_price']:.0f}€ · 💰 прибуток <b>+{r['profit']:.0f}€</b>")
    else:
        lines.append(f"💶 Продати ~{r['sale_price']:.0f}€ · до вигідної ціни: −{r['price'] - r['buy_limit']:.0f}€ "
                     f"(купувати до {r['buy_limit']:.0f}€)")
        if r["has_best_offer"] and r["buy_limit"] > 0:
            lines.append(f"🎯 Можна торгуватись: запропонуй ~{r['buy_limit']:.0f}€")
    basis = basis_line({"sale_source": r.get("sale_source"), "sale_sample": r.get("sale_sample"),
                        "cond_group": r.get("sale_cond"), "spec_group": r.get("sale_spec"),
                        "item_spec": r.get("spec_group")})
    if basis:
        lines.append(basis)
    lines += laptop_warnings(r["title"])
    if pickup_only_items([r["item_id"]]):
        lines.append(PICKUP_NOTE)
    if r.get("url"):
        lines.append(f'<a href="{html.escape(r["url"])}">🔗 Відкрити на eBay</a>')
    return "\n".join(lines)


async def _render_markdowns(update, context, page=0, note=""):
    chat_id = update.effective_chat.id
    rows = await asyncio.to_thread(markdown_list, chat_id)
    last = max(0, (len(rows) - 1) // PER_PAGE)
    page = min(page, last)
    shown = rows[page * PER_PAGE:(page + 1) * PER_PAGE]
    # Перед показом — чи ці оголошення ще продаються
    removed = await run_with_progress(update, context, "📉 ⏳ Перевіряю на eBay, чи оголошення ще продаються…",
                                      recheck_rows, shown) if shown else 0
    if removed:
        rows = await asyncio.to_thread(markdown_list, chat_id)
        page = min(page, max(0, (len(rows) - 1) // PER_PAGE))
        shown = rows[page * PER_PAGE:(page + 1) * PER_PAGE]
        gone = f"🗑 Прибрано вже проданих чи знятих: {removed}"
        note = f"{note}\n{gone}" if note else gone

    pct = get_drop_pct(chat_id)
    pages = max(1, (len(rows) + PER_PAGE - 1) // PER_PAGE)
    lines = [title("📉", "Знизили ціну", count=len(rows),
                   subtitle=f"знижка від {pct:.0f}% · висять від {plural(PRICE_DROP_MIN_DAYS, 'дня', 'днів', 'днів')}"
                   " · не дорожче за ринок" + (f" · сторінка {page + 1} з {pages}" if pages > 1 else ""))]
    if note:
        lines.append(note)
    if not rows:
        lines.append("✨ Поки немає. Бот запам'ятовує першу ціну кожного оголошення і показує тут ті, що довго "
                     "висять і помітно подешевшали — продавець хоче продати, з ним легше торгуватись.\n"
                     "<i>Перші знижки з'являться приблизно через тиждень спостережень.</i>")
    buttons = []
    for i, r in enumerate(shown, 1 + page * PER_PAGE):
        lines.append(_card(i, r))
        buttons.append([
            InlineKeyboardButton(f"🙈 {i} Сховати", callback_data=f"mact:hide:{r['watch_id']}:{r['item_id']}:{page}"),
            InlineKeyboardButton(f"❌ {i} Не той товар", callback_data=f"mact:rej:{r['watch_id']}:{r['item_id']}:{page}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Попередні", callback_data=f"mkd:{page - 1}"))
    if (page + 1) * PER_PAGE < len(rows):
        nav.append(InlineKeyboardButton("Наступні ▶️", callback_data=f"mkd:{page + 1}"))
    if nav:
        buttons.append(nav)
    if rows:
        buttons.append([InlineKeyboardButton("🧹 Очистити список", callback_data="mkdclr")])
    buttons.append(menu_row())
    if shown:
        lines.append(hint("Бот запам'ятовує першу ціну кожного оголошення і показує тут ті, що довго висять і "
                          "помітно подешевшали — з таким продавцем легше торгуватись.\n"
                          "🙈 <b>Сховати</b> — прибрати лише це оголошення.\n"
                          "❌ <b>Не той товар</b> — бот запам'ятає слова з назви.\n"
                          "Поріг знижки і які знижки показувати (лише вигідні / + торг до 15% / усі) — "
                          "у «⚙️ Налаштування» → «📉 Поріг знижки».", "ℹ️ Як це працює"))
    await show_panel(update, context, "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons),
                     parse_mode=ParseMode.HTML)
    mark_track_seen([(r["watch_id"], r["item_id"]) for r in shown])


@require_access
async def markdowns_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mkd:<сторінка>"""
    await _ack_callback(update)
    try:
        page = int(update.callback_query.data.split(":")[1])
    except (IndexError, ValueError):
        page = 0
    await _render_markdowns(update, context, page)


@require_access
async def markdown_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mact:<hide|rej>:<watch_id>:<item_id>:<сторінка>"""
    query_cb = update.callback_query
    _, action, watch_id, rest = query_cb.data.split(":", 3)
    item_id, page = rest.rsplit(":", 1)
    chat_id = update.effective_chat.id
    watch = get_watch(int(watch_id), chat_id)
    row = next((r for r in await asyncio.to_thread(markdown_list, chat_id)
                if r["watch_id"] == int(watch_id) and r["item_id"] == item_id), None)
    if watch is None or row is None:
        await query_cb.answer("Цього оголошення вже немає в списку.")
        return await _render_markdowns(update, context, int(page))
    saved: dict = {}
    note = ""
    if action == "hide":
        await asyncio.to_thread(hide_item, watch, item_id, row["title"], saved)
        await query_cb.answer("🙈 Сховано")
    else:
        words = await asyncio.to_thread(reject_and_learn, watch, item_id, row["title"], saved)
        await query_cb.answer("❌ Прибрано — більше не враховую це оголошення")
        if words:
            note = html.escape(learned_words_note(words))
    undo_record(context, chat_id, "reject", f"{'🙈' if action == 'hide' else '❌'} «{short(row['title'], 28)}»",
                watch_id=watch["id"], item_id=item_id, screen="markdowns", page=int(page), **saved)
    await _render_markdowns(update, context, int(page), note=note)


@require_access
async def drop_pct_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mdpct:show — екран; mdpct:<n> — поріг знижки; mdpct:m:<deal|haggle|all> — які знижки показувати."""
    await _ack_callback(update)
    chat_id = update.effective_chat.id
    parts = update.callback_query.data.split(":")
    note = ""
    if parts[1] == "m" and len(parts) > 2 and parts[2] in MARKDOWN_MODES:
        set_markdown_mode(chat_id, parts[2])
        note = f"✅ Показую: <b>{MODE_LABELS[parts[2]]}</b>"
    elif parts[1] != "show":
        set_drop_pct(chat_id, float(parts[1]))
        note = f"✅ Збережено: знижка від <b>{float(parts[1]):.0f}%</b>"
    current, mode = get_drop_pct(chat_id), get_markdown_mode(chat_id)
    pct_buttons = [InlineKeyboardButton(("✅ " if v == current else "") + f"{v}%", callback_data=f"mdpct:{v}")
                   for v in PRICE_DROP_CHOICES]
    mode_rows = [[InlineKeyboardButton(("✅ " if m == mode else "") + MODE_LABELS[m], callback_data=f"mdpct:m:{m}")]
                 for m in MARKDOWN_MODES]
    rows = [pct_buttons, *mode_rows, [InlineKeyboardButton("◀️ Налаштування", callback_data="menu:ebay_account")]]
    haggle = MARKDOWN_HAGGLE_PCT
    text = (
        note_line(note)
        + title("📉", "Знизили ціну — налаштування", "які оголошення потрапляють у «📉 Знизили ціну»") + "\n\n"
        + f"1️⃣ <b>Поріг знижки: {current:.0f}%</b> <i>(перший ряд кнопок)</i>\n"
        "На скільки продавець має знизити ціну від першої, яку бачив бот. Менший поріг — більше оголошень, "
        "але й більше дрібних знижок.\n\n"
        f"2️⃣ <b>Які знижки показувати: {MODE_LABELS[mode]}</b>\n"
        "• <b>✅ Лише вигідні</b> — тільки ті, які вже зараз вигідно купити за ціною оголошення "
        "(не дорожче за «купувати до»). Найменше оголошень, торгуватися не треба.\n"
        f"• <b>🎯 Вигідні + торг до {haggle}%</b> — ще й ті, до вигідної ціни яких лишається не більше "
        f"{haggle}% ціни оголошення: стільки продавці зазвичай погоджуються скинути на «Preis vorschlagen». "
        f"Напр., MacBook за 1868€, вигідно до 1842€ (−1%) — показую; PS4 за 68€, вигідно до 38€ (−44%) — ні.\n"
        "• <b>📋 Усі</b> — усі помітно здешевілі оголошення, не дорожчі за ринок, навіть якщо до вигідної "
        "ціни дуже далеко. Найбільше оголошень, але багато з них ніколи не стануть вигідними."
    )
    await show_panel(update, context, text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)


@require_access
async def markdowns_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mkdclr — спитати; mkdclr:yes — прибрати весь список (повернеться те, що знову подешевшає)."""
    await _ack_callback(update)
    chat_id = update.effective_chat.id
    if update.callback_query.data != "mkdclr:yes":
        rows = await asyncio.to_thread(markdown_list, chat_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧹 Так, очистити", callback_data="mkdclr:yes")],
            [InlineKeyboardButton("◀️ Ні, назад", callback_data="mkd:0")],
        ])
        await show_panel(update, context,
                         f"🧹 <b>Очистити «📉 Знизили ціну»?</b>\n\nЗі списку зникнуть усі "
                         f"{plural(len(rows), 'оголошення', 'оголошення', 'оголошень')}. Кожне повернеться, "
                         "лише якщо продавець знизить ціну ще раз.",
                         reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return
    rows = await asyncio.to_thread(markdown_list, chat_id)
    await asyncio.to_thread(dismiss_tracks, [(r["watch_id"], r["item_id"], r["price"]) for r in rows])
    await _render_markdowns(update, context, 0,
                            note=f"🧹 Список очищено ({len(rows)}). Оголошення повернуться, якщо подешевшають ще.")
