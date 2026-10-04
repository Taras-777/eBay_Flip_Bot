"""
«📉 Знизили ціну» — оголошення, які довго не продаються і продавець помітно
знизив ціну. Такий продавець хоче позбутися товару — з ним легше торгуватись.

Звідки бот знає першу ціну:
  * ринкові сканування (найновіші оголошення) — ціна одразу після виставлення;
  * окреме сканування найдешевших оголошень (раз на PRICE_DROP_SCAN_HOURS) —
    туди потрапляють і старі оголошення, які після знижки стали дешевими.

Показуються лише оголошення, що висять ≥ PRICE_DROP_MIN_DAYS, подешевшали на
≥ поріг користувача (за замовчуванням 20%) і коштують НЕ дорожче за ринкову
ціну продажу — інакше «знижка» з 900€ до 700€ при ринку 550€ лише засмічувала б список.
"""

import time
from concurrent.futures import ThreadPoolExecutor

from settings import (
    PRICE_DROP_FRESH_HOURS,
    PRICE_DROP_MAX_LOOKUPS,
    PRICE_DROP_MIN_BUDGET,
    PRICE_DROP_MIN_DAYS,
    PRICE_DROP_RECHECK_MINUTES,
    PRICE_DROP_SCAN_HOURS,
    PRICE_DROP_SCAN_PAGES,
    log,
)
from db import (
    get_drop_pct,
    get_market_stats,
    get_meta,
    get_min_profit,
    get_sold_only,
    get_markdown_candidates,
    list_watches,
    mark_track_gone,
    set_meta,
    touch_track,
    track_prices,
    watch_category_ids,
)
from ebay_api import _watch_search_kwargs, browse_budget_left, search_in_categories
from laptops import UNKNOWN_GPU
from market import _annotate_items, _apply_item_filters, _stat_for_item, estimate_resale_profit, max_buy_price

PAGE_SIZE = 200


# ---------- збір цін ----------

def scan_cheapest(watch):
    """Найдешевші оголошення товару (PRICE_DROP_SCAN_PAGES × 200 у кожній категорії) → ціни.
    Повертає кількість запам'ятованих оголошень."""
    found, seen = [], set()
    for cid in (watch_category_ids(watch) or [None]):
        for page in range(PRICE_DROP_SCAN_PAGES):
            stats = {}
            batch = search_in_categories([cid] if cid else [], limit=PAGE_SIZE, offset=page * PAGE_SIZE,
                                         fresh=True, sort="price", stats=stats, **_watch_search_kwargs(watch))
            found += [it for it in batch if it["item_id"] not in seen]
            seen.update(it["item_id"] for it in batch)
            if stats.get("raw", 0) < PAGE_SIZE:
                break   # далі оголошень немає
    _annotate_items(found, max_lookups=PRICE_DROP_MAX_LOOKUPS, watch=watch)
    return track_prices(watch["id"], _apply_item_filters(watch, found))


def run_markdown_scan():
    """Фонова задача: товари, які не сканували понад PRICE_DROP_SCAN_HOURS. Викликати з потоку."""
    now = time.time()
    done = 0
    for w in list_watches():
        key = f"mdscan:{w['id']}"
        if now - float(get_meta(key) or 0) < PRICE_DROP_SCAN_HOURS * 3600:
            continue
        if browse_budget_left() <= PRICE_DROP_MIN_BUDGET:
            break   # ліміт eBay — насамперед для пошуку нових вигідних оголошень
        try:
            scan_cheapest(w)
            done += 1
        except Exception as e:
            log.warning("Не вдалося просканувати найдешевші для «%s»: %s", w["label"], e)
        set_meta(key, int(time.time()))
    if done:
        log.info("📉 Проскановано найдешевші оголошення товарів: %s", done)
    return done


# ---------- список для користувача ----------

def markdown_list(chat_id):
    """Знижки, які варто показати, — від найвигіднішої. Кожна з полями для картки."""
    rows = get_markdown_candidates(chat_id, get_drop_pct(chat_id), PRICE_DROP_MIN_DAYS,
                                   PRICE_DROP_FRESH_HOURS * 3600)
    min_profit = get_min_profit(chat_id)
    sold_only = get_sold_only(chat_id)
    stats_by_watch, result = {}, []
    for r in rows:
        if r["watch_id"] not in stats_by_watch:
            stats_by_watch[r["watch_id"]] = {(s["cond_group"], s["spec_group"]): s
                                             for s in get_market_stats(r["watch_id"])}
        if (r["spec_group"] or "").startswith(UNKNOWN_GPU):
            continue   # ноутбук з невідомою відеокартою — ціну ні з чим порівняти
        stat = _stat_for_item(stats_by_watch[r["watch_id"]], r, sold_only=sold_only)
        if stat is None:
            continue   # ринкова ціна ще не відома — не з чим порівняти
        sale = stat["sale_price"] or stat["median_price"]
        if r["price"] > sale:
            continue   # навіть після знижки дорожче за ринок
        _, profit = estimate_resale_profit(sale, r["price"])
        r.update(sale_price=sale, profit=profit, buy_limit=max_buy_price(sale, min_profit),
                 drop_pct=(r["first_price"] - r["price"]) / r["first_price"] * 100,
                 # звідки ціна «продати» — для рядка «📊 Ціна продажу …» у картці
                 sale_source=stat["sale_source"], sale_sample=stat["sample_size"],
                 sale_cond=stat["cond_group"], sale_spec=stat["spec_group"])
        result.append(r)
    result.sort(key=lambda r: -r["profit"])
    return result


def markdown_counts(chat_id):
    """(усього, нових) — для кнопки в головному меню."""
    try:
        rows = markdown_list(chat_id)
    except Exception as e:   # лічильник у меню не має ламати саме меню
        log.debug("Не вдалося порахувати знижки: %s", e)
        return 0, 0
    return len(rows), sum(1 for r in rows if r["seen_at"] is None)


def recheck_rows(rows):
    """Перед показом: чи оголошення ще продаються (ті, що давно не бачили). Викликати з потоку.
    Повертає кількість прибраних."""
    from deal_check import listing_available   # тут, щоб не було циклу імпортів
    stale = [r for r in rows if r["last_seen"] < time.time() - PRICE_DROP_RECHECK_MINUTES * 60]
    if not stale:
        return 0

    def check(r):
        available = listing_available(r["item_id"])
        if available is False:
            mark_track_gone(r["watch_id"], r["item_id"])
            return 1
        if available:
            touch_track(r["watch_id"], r["item_id"])
        return 0

    with ThreadPoolExecutor(max_workers=min(5, len(stale))) as pool:
        return sum(pool.map(check, stale))
