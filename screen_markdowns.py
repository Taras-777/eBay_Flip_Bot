"""
«📉 Знизили ціну» і «⚙️ Поріг знижки».
"""

import asyncio
import html
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from settings import PRICE_DROP_CHOICES, PRICE_DROP_MIN_DAYS
from deal_reprice import basis_line, specs_line
from db import dismiss_tracks, get_drop_pct, get_watch, mark_track_seen, pickup_only_items, set_drop_pct
from learning import hide_item, learned_words_note, reject_and_learn
from laptops import laptop_warnings
from markdowns import markdown_list, recheck_rows
from panel import _ack_callback, run_with_progress, show_panel
from access import require_access
from textparse import PICKUP_NOTE, plural
from undo import record as undo_record, short

PER_PAGE = 5


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
        html.escape((r["title"] or "")[:90]),
        *([html.escape(sp)] if (sp := specs_line(r["item_id"], r["title"], r.get("spec_group"))) else []),
        f"📉 було {r['first_price']:.0f}€ → зараз <b>{r['price']:.0f}€</b> (−{r['drop_pct']:.0f}%){changed}",
    ]
    if r["price"] <= r["buy_limit"]:
        lines.append(f"✅ Вже вигідно: продати ~{r['sale_price']:.0f}€ · 💰 прибуток ~<b>{r['profit']:.0f}€</b>")
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
    lines = [f"📉 <b>Знизили ціну</b> ({len(rows)}) · знижка від {pct:.0f}%, висять від "
             f"{plural(PRICE_DROP_MIN_DAYS, 'дня', 'днів', 'днів')}, не дорожче за ринок"]
    if note:
        lines.append(note)
    if not rows:
        lines.append("Поки немає. Бот запам'ятовує першу ціну кожного оголошення і показує тут ті, що довго "
                     "висять і помітно подешевшали — продавець хоче продати, з ним легше торгуватись.\n"
                     "<i>Перші знижки з'являться приблизно через тиждень спостережень.</i>")
    buttons = []
    for i, r in enumerate(shown, 1 + page * PER_PAGE):
        lines.append(_card(i, r))
        buttons.append([
            InlineKeyboardButton(f"🙈 {i}", callback_data=f"mact:hide:{r['watch_id']}:{r['item_id']}:{page}"),
            InlineKeyboardButton(f"❌ {i} Інший товар", callback_data=f"mact:rej:{r['watch_id']}:{r['item_id']}:{page}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data=f"mkd:{page - 1}"))
    if (page + 1) * PER_PAGE < len(rows):
        nav.append(InlineKeyboardButton("Далі ▶️", callback_data=f"mkd:{page + 1}"))
    if nav:
        buttons.append(nav)
    if rows:
        buttons.append([InlineKeyboardButton("🧹 Очистити список", callback_data="mkdclr")])
    buttons.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:home")])
    if shown:
        lines.append("<i>🙈 — сховати це оголошення; ❌ Інший товар — не той товар, бот запам'ятає.</i>")
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
    """mdpct:show — вибір порогу; mdpct:<n> — зберегти."""
    await _ack_callback(update)
    chat_id = update.effective_chat.id
    choice = update.callback_query.data.split(":")[1]
    note = ""
    if choice != "show":
        set_drop_pct(chat_id, float(choice))
        note = f"✅ Збережено: знижка від <b>{float(choice):.0f}%</b>\n\n"
    current = get_drop_pct(chat_id)
    buttons = [InlineKeyboardButton(("✅ " if v == current else "") + f"{v}%", callback_data=f"mdpct:{v}")
               for v in PRICE_DROP_CHOICES]
    await show_panel(
        update, context,
        f"{note}⚙️ <b>Поріг знижки</b>: зараз <b>{current:.0f}%</b>\n\n"
        "У «📉 Знизили ціну» потрапляють оголошення, ціну яких знизили щонайменше на стільки "
        "від першої ціни, яку бачив бот. Менший поріг — більше оголошень, але й більше дрібних знижок.",
        reply_markup=InlineKeyboardMarkup([buttons, [InlineKeyboardButton("◀️ До налаштувань", callback_data="menu:ebay_account")]]),
        parse_mode=ParseMode.HTML,
    )


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
