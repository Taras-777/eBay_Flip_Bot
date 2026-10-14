"""
«🔨 Аукціони» — вмикається в «⚙️ Налаштування» (за замовчуванням вимкнено).

Окремий пошук аукціонів кожного товару, що закінчуються через 6–12 год (AUCTION_WINDOW_HOURS),
раз на AUCTION_SCAN_HOURS. Поточна ставка + доставка порівнюється з «купувати до» тієї ж групи,
що й для звичайних оголошень. Бот НЕ ставить ставок (API ставок eBay — лише для партнерів, а
автоматизація сайту порушує правила eBay) — лише два сповіщення:
  1. знайшов вигідний аукціон: ставка, «купувати до», яку максимальну ставку можна поставити;
  2. за AUCTION_FINAL_MINUTES до кінця — перевірена поточна ставка: ще вигідно (нове сповіщення)
     чи вже ні (тоді лише тихо дописується в перше повідомлення, без дзвіночка).
"""

import asyncio
import html
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from settings import (
    AUCTION_FINAL_LOOP_SECONDS,
    AUCTION_FINAL_MINUTES,
    AUCTION_MAX_LOOKUPS,
    AUCTION_MIN_BUDGET,
    AUCTION_SCAN_HOURS,
    AUCTION_WINDOW_HOURS,
    LAPTOP_SALES_WINDOW_DAYS,
    LOCAL_TZ,
    SALES_WINDOW_DAYS,
    SEARCH_RESERVE,
    log,
)
from db import (
    add_auction_alert,
    auction_chats,
    cleanup_auction_alerts,
    due_auction_finals,
    get_alerted_auctions,
    get_auctions_enabled,
    get_market_stats,
    get_meta,
    get_min_profit,
    get_sold_listings,
    get_sold_only,
    list_watches,
    mark_auction_final,
    set_auction_message,
    set_meta,
    watch_category_ids,
)
from ebay_api import _watch_search_kwargs, browse_budget_left, fetch_item_by_legacy_id, search_in_categories
from market import _annotate_items, _apply_item_filters, _stat_for_item, estimate_resale_profit, laptop_unknown, \
    max_buy_price
from sales import is_slow_seller
from textparse import CURRENCY_TO_EUR, PICKUP_NOTE

PAGE_SIZE = 200


def _when(ts):
    """«сьогодні о 21:40» / «завтра о 08:15»."""
    at = datetime.fromtimestamp(ts, LOCAL_TZ)
    days = (at.date() - datetime.now(LOCAL_TZ).date()).days
    day = "сьогодні" if days <= 0 else "завтра" if days == 1 else at.strftime("%d.%m")
    return f"{day} о {at:%H:%M}"


def _left(ts, now=None):
    minutes = max(0, int((ts - (now or time.time())) / 60))
    if minutes < 60:
        return f"{minutes} хв"
    return f"{minutes // 60} год {minutes % 60:02d} хв"


def first_text(w, it, sale, buy_limit, now=None):
    """Перше сповіщення: знайдено вигідний аукціон."""
    ship = it.get("shipping_cost") or 0
    bid = it["total_price"] - ship
    _, profit = estimate_resale_profit(sale, it["total_price"])
    bids = it.get("bid_count")
    lines = [f"🔨 <b>Вигідний аукціон — {html.escape(w['label'])}</b>",
             html.escape(it.get("title") or ""),
             "",
             f"Ставка зараз: <b>{bid:.0f}€</b>" + (f" + доставка {ship:.0f}€ = {it['total_price']:.0f}€" if ship else "")
             + (f" · ставок: {bids}" if bids is not None else ""),
             f"Купувати до: {buy_limit:.0f}€ → <b>ставити не більше {buy_limit - ship:.0f}€</b>",
             f"Продати ~{sale:.0f}€ → прибуток за поточної ставки ~{profit:.0f}€",
             f"⏰ Закінчиться {_when(it['end_at'])} (через {_left(it['end_at'], now)})"]
    if it.get("suspicious"):
        lines.append("⚠️ У продавця мало відгуків або низький рейтинг")
    if it.get("pickup_only"):
        lines.append(PICKUP_NOTE)
    lines.append(f"\n<i>Ставка ще зросте. Нагадаю за {AUCTION_FINAL_MINUTES} хв до кінця з актуальною ставкою.</i>")
    return "\n".join(lines)


def _markup(url):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Відкрити на eBay", url=url)]]) if url else None


# ---------- пошук ----------

def scan_watch_auctions(w, now=None):
    """Аукціони товару, що закінчуються через 6–12 год і зараз вигідні. Нові записуються в базу.
    Повертає [(аукціон, текст)] — для сповіщень. Викликати з потоку."""
    now = now or time.time()
    start, end = (now + h * 3600 for h in AUCTION_WINDOW_HOURS)
    found, seen = [], set()
    for cid in (watch_category_ids(w) or [None]):
        batch = search_in_categories([cid] if cid else [], limit=PAGE_SIZE, fresh=True, sort="endingSoonest",
                                     auctions=True, end_between=(start, end), **_watch_search_kwargs(w))
        found += [it for it in batch if it["item_id"] not in seen]
        seen.update(it["item_id"] for it in batch)
    found = [it for it in found if it.get("end_at") and start - 600 <= it["end_at"] <= end + 600]
    already = get_alerted_auctions(w["chat_id"], [it["item_id"] for it in found])
    found = [it for it in found if it["item_id"] not in already]
    if not found:
        return []
    stats = {(r["cond_group"], r["spec_group"]): r for r in get_market_stats(w["id"])}
    if not stats:
        return []   # ринок товару ще не проаналізовано — не з чим порівнювати
    _annotate_items(found, max_lookups=AUCTION_MAX_LOOKUPS, watch=w)
    found = _apply_item_filters(w, found)
    min_profit = get_min_profit(w["chat_id"])
    sold_only = get_sold_only(w["chat_id"])
    sold = get_sold_listings(w["id"], max(SALES_WINDOW_DAYS, LAPTOP_SALES_WINDOW_DAYS))
    result = []
    for it in found:
        if laptop_unknown(it):
            continue   # клас ноутбука ще невідомий — наступного сканування
        stat = _stat_for_item(stats, it, sold_only=sold_only)
        if stat is None:
            continue
        sale = stat["sale_price"] or stat["median_price"]
        buy_limit = max_buy_price(sale, min_profit)
        if it["total_price"] > buy_limit:
            continue
        if is_slow_seller(sold, it["cond_group"], it["spec_group"]):
            continue   # дешево, але така конфігурація не продається
        text = first_text(w, it, sale, buy_limit, now)
        ship = it.get("shipping_cost") or 0
        if add_auction_alert(w["chat_id"], it["item_id"], w["id"], it["end_at"], buy_limit, ship,
                             it["total_price"] - ship, text, it.get("url")):
            result.append(({"chat_id": w["chat_id"], "item_id": it["item_id"], "url": it.get("url")}, text))
    return result


def run_auction_scan(now=None):
    """Товари чатів з увімкненими аукціонами, які не сканували понад AUCTION_SCAN_HOURS.
    Викликати з потоку. Повертає [(аукціон, текст)]."""
    now = now or time.time()
    chats = auction_chats()
    if not chats:
        return []
    alerts = []
    for w in list_watches():
        if w["chat_id"] not in chats:
            continue
        key = f"aucscan:{w['id']}"
        if now - float(get_meta(key) or 0) < AUCTION_SCAN_HOURS * 3600:
            continue
        if browse_budget_left() <= AUCTION_MIN_BUDGET:
            break   # ліміт eBay — насамперед для звичайного пошуку вигідних оголошень
        try:
            alerts += scan_watch_auctions(w, now)
        except Exception as e:
            log.warning("Не вдалося знайти аукціони для «%s»: %s", w["label"], e)
        set_meta(key, int(time.time()))
    if alerts:
        log.info("🔨 Знайдено вигідних аукціонів: %s", len(alerts))
    return alerts


async def run_auction_cycle(app):
    """Фонова задача циклу планувальника: пошук → сповіщення."""
    alerts = await asyncio.to_thread(run_auction_scan)
    chats = set()
    for a, text in alerts:
        try:
            msg = await app.bot.send_message(chat_id=a["chat_id"], text=text, parse_mode=ParseMode.HTML,
                                             reply_markup=_markup(a["url"]), disable_web_page_preview=True)
            await asyncio.to_thread(set_auction_message, a["chat_id"], a["item_id"], getattr(msg, "message_id", None))
            chats.add(a["chat_id"])
        except Exception as e:
            log.warning("Не вдалося надіслати сповіщення про аукціон у чат %s: %s", a["chat_id"], e)
    await _repost(app, chats)


async def _repost(app, chats):
    if not chats:
        return
    from panel import repost_panel   # тут, щоб не було циклу імпортів
    for chat_id in chats:
        await repost_panel(app, chat_id)


# ---------- друге сповіщення: перед кінцем ----------

def current_bid(item_id):
    """(стан, ставка в EUR, ставок): стан 'active' / 'gone' (завершено чи знято) / 'unknown'."""
    if browse_budget_left() <= SEARCH_RESERVE:
        return "unknown", None, None
    from trading_api import legacy_item_id   # тут, щоб не було циклу імпортів
    try:
        data = fetch_item_by_legacy_id(legacy_item_id(item_id))
    except Exception as e:
        log.debug("Не вдалося перевірити аукціон %s: %s", item_id, e)
        return "unknown", None, None
    if data is None:
        return "gone", None, None
    bid_info = data.get("currentBidPrice") or data.get("price") or {}
    try:
        bid = float(bid_info.get("value")) * CURRENCY_TO_EUR.get((bid_info.get("currency") or "EUR").upper(), 1.0)
    except (TypeError, ValueError):
        bid = None
    try:
        bids = int(data["bidCount"]) if data.get("bidCount") is not None else None
    except (TypeError, ValueError):
        bids = None
    end = data.get("itemEndDate")
    if end:
        from ebay_api import _parse_ebay_ts
        end_ts = _parse_ebay_ts(end)
        if end_ts and end_ts <= time.time():
            return "gone", bid, bids
    return "active", bid, bids


def final_decision(a, state, bid, bids, now=None):
    """Що робити перед кінцем аукціону a: ('send', текст) — нове сповіщення;
    ('note', дописка) — тихо дописати в перше повідомлення."""
    left = f"через {_left(a['end_at'], now)}"
    if state == "gone":
        return "note", "❌ Аукціон уже завершено або знято."
    if state == "unknown" or bid is None:
        return "send", (f"⏰ Аукціон закінчується {_when(a['end_at'])} ({left}).\n"
                        f"Не вдалося перевірити ставку — подивись на eBay. "
                        f"Ставити не більше {a['buy_limit'] - a['shipping']:.0f}€.")
    total = bid + (a["shipping"] or 0)
    if total > a["buy_limit"]:
        return "note", f"❌ Ставка вже {bid:.0f}€ — дорожче за «купувати до» ({a['buy_limit']:.0f}€ з доставкою)."
    bids_txt = f" · ставок: {bids}" if bids is not None else ""
    return "send", (f"⏰ <b>Аукціон закінчується {_when(a['end_at'])} ({left})</b>\n"
                    f"Ставка зараз: <b>{bid:.0f}€</b>{bids_txt}\n"
                    f"✅ Ще вигідно — ставити не більше <b>{a['buy_limit'] - a['shipping']:.0f}€</b>.")


async def check_finals(app, now=None):
    """Аукціони, яким до кінця ≤ AUCTION_FINAL_MINUTES: перевірити ставку й сповістити."""
    due = await asyncio.to_thread(due_auction_finals, AUCTION_FINAL_MINUTES, now)
    chats = set()
    for a in due:
        if not await asyncio.to_thread(get_auctions_enabled, a["chat_id"]):
            await asyncio.to_thread(mark_auction_final, a["chat_id"], a["item_id"])
            continue   # аукціони вимкнули — більше не турбуємо
        state, bid, bids = await asyncio.to_thread(current_bid, a["item_id"])
        action, text = final_decision(a, state, bid, bids, now)
        await asyncio.to_thread(mark_auction_final, a["chat_id"], a["item_id"], bid)
        try:
            if action == "send":
                await app.bot.send_message(chat_id=a["chat_id"], text=text, parse_mode=ParseMode.HTML,
                                           reply_to_message_id=a.get("message_id"), reply_markup=_markup(a.get("url")),
                                           disable_web_page_preview=True, allow_sending_without_reply=True)
                chats.add(a["chat_id"])
            elif a.get("message_id") and a.get("text"):
                await app.bot.edit_message_text(f"{a['text']}\n\n{html.escape(text)}", chat_id=a["chat_id"],
                                                message_id=a["message_id"], parse_mode=ParseMode.HTML,
                                                reply_markup=_markup(a.get("url")), disable_web_page_preview=True)
        except Exception as e:
            log.warning("Не вдалося надіслати друге сповіщення про аукціон %s: %s", a["item_id"], e)
    await _repost(app, chats)


async def auction_final_loop(app):
    """Окремий цикл (не залежить від інтервалу пошуку): раз на кілька хвилин — лише база;
    запит до eBay — тільки для аукціону, якому саме час нагадати."""
    last_cleanup = 0.0
    try:
        while True:
            try:
                await check_finals(app)
                if time.time() - last_cleanup > 86400:
                    await asyncio.to_thread(cleanup_auction_alerts)
                    last_cleanup = time.time()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Помилка перевірки аукціонів перед кінцем — продовжую")
            await asyncio.sleep(AUCTION_FINAL_LOOP_SECONDS)
    except asyncio.CancelledError:
        raise
