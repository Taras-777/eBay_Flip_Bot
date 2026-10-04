"""
«🔥 Вигідні пропозиції» і «⚙️ Мін. прибуток».
"""

import asyncio
import html
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from settings import LAPTOP_SALES_WINDOW_DAYS, MIN_PROFIT_CHOICES, SALES_WINDOW_DAYS
from learning import hide_item, learned_words_note, reject_and_learn
from db import (
    get_deal,
    get_track_row,
    get_inbox_deals,
    mark_deals_seen,
    clear_inbox_deals,
    inbox_deal_ids,
    get_min_profit,
    set_min_profit,
    get_sold_listings,
    get_watch,
    set_deal_status,
)
from market import estimate_resale_profit
from panel import _ack_callback, run_with_progress, show_panel
from access import require_access
from sales import sales_note
from deal_check import recheck_shown_deals
from deal_reprice import basis_line, reprice_deals
from laptops import laptop_warnings
from textparse import is_bundle
from undo import record as undo_record, short


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
    rows.append([InlineKeyboardButton("◀️ До пропозицій", callback_data="deals:0")])
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


def _deal_card(n, d, sold=None):
    sale = d["median_price"]
    note = sales_note(sold, d["cond_group"], d["spec_group"]) if sold and d.get("cond_group") else ""
    _, profit = estimate_resale_profit(sale, d["total_price"])
    new = "🆕 " if d["seen_at"] is None else ""
    offer = " · 🎯 можна торгуватись" if d["has_best_offer"] else ""
    if is_bundle(d["title"]):
        offer += " · 📦 комплект"
    if d.get("watch_count"):
        offer += f" · 👁 стежать: {d['watch_count']}"
    warn = "\n⚠️ Мало відгуків у продавця — перевір уважно" if d["suspicious"] else ""
    for w in laptop_warnings(d["title"]):
        warn += f"\n{w}"
    track = get_track_row(d["watch_id"], d["item_id"])
    if track and track["first_price"] >= d["total_price"] * 1.05:
        cut = (track["first_price"] - d["total_price"]) / track["first_price"] * 100
        warn += (f"\n📉 Продавець уже знизив ціну на {cut:.0f}% (було {track['first_price']:.0f}€) — "
                 "ймовірно, погодиться поторгуватись")
    if d.get("auction"):
        warn += "\n" + auction_note(d.get("current_bid"), d.get("bid_count"), d.get("end_at"),
                                    as_of="(на момент знахідки)")
    basis = basis_line(d)
    listed = f"📅 виставлено {_listed(d['listed_at'])}\n" if d.get("listed_at") else ""
    return (f"<b>{n}. {new}{html.escape(d['watch_label'])}</b> · знайдено {_when(d['created_at'])}\n"
            f"{html.escape(d['title'][:90])}\n{listed}"
            f"💶 <b>{d['total_price']:.0f}€</b> → продати ~{sale:.0f}€ · 💰 прибуток ~<b>{profit:.0f}€</b>{offer}\n"
            + (f"{html.escape(basis)}\n" if basis else "") + (f"{warn.lstrip()}\n" if warn else "")
            + (f"{html.escape(note)}\n" if note else "") +
            f'<a href="{html.escape(d["url"])}">🔗 Відкрити на eBay</a>')


async def _render_deals(update, context, page=0, note=""):
    chat_id = update.effective_chat.id
    # «Продати» — за теперішньою статистикою ринку, а не за тією, що була в момент знахідки
    repriced = await run_with_progress(update, context, "🔥 ⏳ Перераховую ціни…", reprice_deals, chat_id)
    if repriced:
        reprice_note = (f"🔄 Ціни продажу перераховано за свіжими даними — прибрано невигідних: {repriced}")
        note = f"{note}\n{reprice_note}" if note else reprice_note
    deals, total = get_inbox_deals(chat_id, limit=DEALS_PER_PAGE, offset=page * DEALS_PER_PAGE)
    # Перед показом — чи ці лоти ще продаються (вигідні розкуповують швидко)
    removed = await run_with_progress(update, context, "🔥 ⏳ Перевіряю на eBay, чи оголошення ще продаються…",
                                      recheck_shown_deals, [d["id"] for d in deals]) if deals else 0
    if deals:   # свіжі дані після перевірки (зокрема «👁 стежать»)
        deals, total = get_inbox_deals(chat_id, limit=DEALS_PER_PAGE, offset=page * DEALS_PER_PAGE)
    if removed:
        gone_note = f"🗑 Прибрано вже проданих чи знятих: {removed}"
        note = f"{note}\n{gone_note}" if note else gone_note
    if not deals and page > 0:
        page = max(0, (total - 1) // DEALS_PER_PAGE)
        deals, total = get_inbox_deals(chat_id, limit=DEALS_PER_PAGE, offset=page * DEALS_PER_PAGE)
    lines = [f"🔥 <b>Вигідні пропозиції</b> ({total}) · прибуток від {get_min_profit(chat_id):.0f}€, "
             "найвигідніші вгорі. Продані й зняті оголошення бот прибирає сам."]
    if note:
        lines.append(note)
    rows = []
    if not deals:
        lines.append("Поки нових немає. Щойно бот знайде вигідну пропозицію — прийде одне коротке "
                     "сповіщення, а сама пропозиція з'явиться тут.")
    sold_by_watch = {}
    for i, d in enumerate(deals, 1 + page * DEALS_PER_PAGE):
        if d["watch_id"] not in sold_by_watch:
            sold_by_watch[d["watch_id"]] = get_sold_listings(d["watch_id"], max(SALES_WINDOW_DAYS, LAPTOP_SALES_WINDOW_DAYS))
        lines.append(_deal_card(i, d, sold_by_watch[d["watch_id"]]))
        rows.append([
            InlineKeyboardButton(f"✅ {i} Куплено", callback_data=f"dact:buy:{d['id']}:{page}"),
            InlineKeyboardButton(f"🙈 {i}", callback_data=f"dact:hide:{d['id']}:{page}"),
            InlineKeyboardButton(f"❌ {i} Інший товар", callback_data=f"dact:rej:{d['id']}:{page}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data=f"deals:{page - 1}"))
    if (page + 1) * DEALS_PER_PAGE < total:
        nav.append(InlineKeyboardButton("Далі ▶️", callback_data=f"deals:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(f"⚙️ Мін. прибуток: {get_min_profit(chat_id):.0f}€", callback_data="mprof:show")])
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
    saved: dict = {}
    chat_id = update.effective_chat.id
    if action == "buy":
        set_deal_status(deal["id"], "bought")
        undo_record(context, chat_id, "deal_status", f"✅ Куплено «{short(deal['title'], 28)}»",
                    deal_ids=[deal["id"]], status="new", screen="deals", page=int(page))
        await query_cb.answer("✅ Позначено як куплене")
        note = f"✅ «{html.escape(deal['title'][:50])}» — куплено"
    elif action == "hide":
        await asyncio.to_thread(hide_item, watch, deal["item_id"], deal["title"], saved)
        set_deal_status(deal["id"], "skipped")
        undo_record(context, chat_id, "reject", f"🙈 «{short(deal['title'], 28)}»", watch_id=watch["id"],
                    item_id=deal["item_id"], deal_id=deal["id"], screen="deals", page=int(page), **saved)
        await query_cb.answer("🙈 Сховано")
    else:
        words = await asyncio.to_thread(reject_and_learn, watch, deal["item_id"], deal["title"], saved)
        set_deal_status(deal["id"], "skipped")
        undo_record(context, chat_id, "reject", f"❌ «{short(deal['title'], 28)}»", watch_id=watch["id"],
                    item_id=deal["item_id"], deal_id=deal["id"], screen="deals", page=int(page), **saved)
        await query_cb.answer("❌ Прибрано — більше не враховую це оголошення")
        if words:
            note = html.escape(learned_words_note(words))
    await _render_deals(update, context, int(page), note=note)


@require_access
async def deals_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ack_callback(update)
    ids = inbox_deal_ids(update.effective_chat.id)
    clear_inbox_deals(update.effective_chat.id)
    if ids:
        undo_record(context, update.effective_chat.id, "deal_status", f"🧹 очищення списку ({len(ids)})",
                    deal_ids=ids, status="new", screen="deals", page=0)
    await _render_deals(update, context, 0, note="🧹 Список очищено")


# Імпорти з інших екранів — унизу, щоб модулі могли посилатися один на одного
from screen_common import (  # noqa: E402
    _listed,
    _when,
    auction_note,
)
