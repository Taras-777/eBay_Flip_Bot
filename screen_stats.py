"""
«📊 Статистика»: наскільки «живі» товари — і ті, що ти відстежуєш, і ті, що в «💡 Що перепродавати».

Для кожного товару: скільки оголошень бот бачив від початку стеження (і скільки з них нових за
24 години), скільки продано (і скільки за 24 години), як змінилась типова ціна за тиждень.
Усе — з бази, без запитів до eBay.
"""

import html
import statistics
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from settings import LISTING_OBS_RETENTION_DAYS
from db import (
    discovery_activity,
    get_conn,
    get_deal_stats,
    get_hidden_candidates,
    get_price_history,
    list_watches,
    watch_activity,
)
from discovery import CANDIDATES, tracked_candidates
from sales import weekly_change
from panel import _ack_callback, show_panel
from access import require_access

DAY = 86400
DISCOVERY_PER_PAGE = 15
TREND_MIN_PCT = 3        # меншу зміну ціни не показуємо — це шум
TREND_MIN_SAMPLE = 5


def _trend(pct):
    if pct is None or abs(pct) < TREND_MIN_PCT:
        return ""
    return f" · {'📈' if pct > 0 else '📉'} {pct:+.0f}%"


def _watch_trend(watch_id):
    """Зміна типової ціни за тиждень — основна група товару (вживані, якщо є)."""
    history = get_price_history(watch_id)
    for key in (("used", "*"), ("new", "*")):
        change = weekly_change(history.get(key))
        if change:
            return change[0]
    return None


def _discovery_trends():
    """{товар «💡»: зміна ціни за тиждень у %} — медіана оголошень, які з'явились за останні 7 днів,
    проти тих, що з'явились 7–14 днів тому (окремої історії цін у «💡» немає)."""
    now = time.time()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT candidate, price, first_seen FROM discovery_obs WHERE first_seen >= ? AND price IS NOT NULL",
            (now - 14 * DAY,)).fetchall()
    recent, before = {}, {}
    for r in rows:
        (recent if r["first_seen"] >= now - 7 * DAY else before).setdefault(r["candidate"], []).append(r["price"])
    result = {}
    for name, prices in recent.items():
        old = before.get(name) or []
        if len(prices) >= TREND_MIN_SAMPLE and len(old) >= TREND_MIN_SAMPLE:
            old_median = statistics.median(old)
            if old_median:
                result[name] = (statistics.median(prices) - old_median) / old_median * 100
    return result


def _row(icon, name, a, pct):
    new = f" (🆕 +{a['new']})" if a["new"] else ""
    sold_new = f" (🆕 +{a['sold_new']})" if a["sold_new"] else ""
    return (f"{icon} <b>{html.escape(name)}</b>\n"
            f"   оголошень {a['total']}{new} · продано {a['sold']}{sold_new}{_trend(pct)}")


def _activity_key(item):
    a = item[2]
    return (-a["sold_new"], -a["new"], -a["sold"])


HEADER = ("<i>🆕 — за останні 24 години. Оголошень і продажів — від початку стеження "
          f"(бот пам'ятає {LISTING_OBS_RETENTION_DAYS} днів). 📈/📉 — типова ціна за тиждень.</i>")


def stats_text(chat_id):
    """Головний екран: лише твої товари і підсумок «🔥 Знахідки»."""
    since = time.time() - DAY
    watches = list_watches(chat_id=chat_id, active_only=True)
    act = watch_activity([w["id"] for w in watches], since)
    mine = sorted(((w["label"], w["id"], act.get(w["id"]) or {"total": 0, "new": 0, "sold": 0, "sold_new": 0})
                   for w in watches), key=_activity_key)
    lines = ["📊 <b>Статистика</b>", HEADER, "📦 <b>Мої товари</b>"]
    lines += [_row("📌", label, a, _watch_trend(wid)) for label, wid, a in mine] or ["Товарів ще немає."]
    deals = get_deal_stats(chat_id)
    if deals:
        lines.append(f"🔥 Знахідки: ✅ куплено {deals.get('bought', 0)} · ❌ пропущено {deals.get('skipped', 0)}"
                     f" · 🆕 без рішення {deals.get('new', 0)}")
    return "\n\n".join(lines)


def discovery_stats_text(chat_id, page=0):
    """(текст, сторінок усього, сторінка) — статистика «💡 Що перепродавати», по DISCOVERY_PER_PAGE.
    Без прихованих (🙈) і без тих, що вже є у «📦 Мої товари» (вони — на головному екрані)."""
    skip = set(get_hidden_candidates(chat_id)) | tracked_candidates(chat_id)
    disc_act = discovery_activity(time.time() - DAY)
    trends = _discovery_trends()
    emoji = {name: e for e, name, _, _ in CANDIDATES}
    disc = sorted(((name, name, disc_act[name]) for name in emoji if name in disc_act and name not in skip),
                  key=_activity_key)
    pages = max(1, (len(disc) + DISCOVERY_PER_PAGE - 1) // DISCOVERY_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    title = "📊 <b>Статистика · 💡 Що перепродавати</b>" + (f" · сторінка {page + 1} з {pages}" if pages > 1 else "")
    lines = [title, HEADER]
    start = page * DISCOVERY_PER_PAGE
    shown = disc[start:start + DISCOVERY_PER_PAGE]
    lines += [_row(emoji[name], name, a, trends.get(name)) for name, _, a in shown] or [
        "Даних ще немає — з'являться після першого аналізу."]
    return "\n\n".join(lines), pages, page


@require_access
async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """menu:stats або /stats — мої товари; stats:d<n> — сторінка n статистики «💡»."""
    data = update.callback_query.data if update.callback_query else ""
    if data.startswith("stats:d") and data[7:].isdigit():
        await show_discovery_stats(update, context, int(data[7:]))
    else:
        await show_stats(update, context)


async def show_stats(update, context):
    await _ack_callback(update)
    text = stats_text(update.effective_chat.id)
    rows = [[InlineKeyboardButton("💡 Статистика «Що перепродавати»", callback_data="stats:d0")],
            [InlineKeyboardButton("◀️ Меню", callback_data="menu:home")]]
    await show_panel(update, context, text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)


async def show_discovery_stats(update, context, page=0):
    await _ack_callback(update)
    text, pages, page = discovery_stats_text(update.effective_chat.id, page)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Попередні", callback_data=f"stats:d{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Наступні ▶️", callback_data=f"stats:d{page + 1}"))
    rows = ([nav] if nav else []) + [[InlineKeyboardButton("◀️ До статистики", callback_data="menu:stats")]]
    await show_panel(update, context, text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
