"""
«💡 Що перепродавати».
"""

import asyncio
import html
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from settings import (
    DEFAULT_CONDITION_IDS,
    LOCAL_TZ,
    is_owner,
    log,
)
from discovery import run_discovery, top_recommendations
from db import add_watch, find_duplicate_watch, get_watch
from market import _recalculate_watch_medians
from panel import show_panel
from access import require_access
from shared_market import attach_shared_history
from sales import speed_text


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
        categories=[],
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


# Імпорти з інших екранів — унизу, щоб модулі могли посилатися один на одного
from screen_watch import (  # noqa: E402
    _show_watch_details,
)
