"""
«🔥 Вигідні пропозиції» і «⚙️ Мін. прибуток».
"""

import asyncio
import html
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from settings import MIN_PROFIT_CHOICES, SALES_WINDOW_DAYS
from learning import hide_item, learned_words_note, reject_and_learn
from db import (
    get_deal,
    get_inbox_deals,
    mark_deals_seen,
    clear_inbox_deals,
    get_min_profit,
    set_min_profit,
    get_sold_listings,
    get_watch,
    set_deal_status,
)
from market import estimate_resale_profit
from panel import _ack_callback, show_panel
from access import require_access
from sales import sales_note


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
    lines = [f"🔥 <b>Вигідні пропозиції</b> ({total}) · прибуток від {get_min_profit(chat_id):.0f}€, "
             "найвигідніші вгорі"]
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


# Імпорти з інших екранів — унизу, щоб модулі могли посилатися один на одного
from screen_common import (  # noqa: E402
    _when,
)
