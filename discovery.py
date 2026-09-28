"""
«💡 Що перепродавати»: бот сам аналізує популярні для перепродажу товари,
яких ти ще не відстежуєш, і складає рейтинг найперспективніших.

Раз на DISCOVERY_INTERVAL_HOURS для кожного кандидата — один запит до eBay
(200 найновіших оголошень). З них рахується:
  * типова ціна і реалістична ціна продажу (як для звичайних товарів);
  * скільки оголошень зараз дешевші за «купувати до» і який прибуток вони дають
    (після комісії eBay і доставки), і як часто такі з'являються;
  * швидкість продажу — скільки оголошень на добу зникає (ймовірно, куплені);
    з'являється після кількох днів спостережень.

Список кандидатів — CANDIDATES нижче, його можна доповнювати.
"""

import statistics
import time
from collections import Counter

from settings import DEFAULT_CONDITION_IDS, SEARCH_RESERVE, log
from textparse import _search_tokens, extract_spec_key, is_accessory_category
from db import (
    discovery_gone_stats,
    get_discovery_results,
    get_meta,
    list_watches,
    save_discovery_result,
    set_meta,
    update_discovery_obs,
)
from ebay_api import browse_budget_left, search_active_items
from market import estimate_resale_profit, filter_outliers, max_buy_price, percentile

DISCOVERY_INTERVAL_HOURS = 12
DISCOVERY_MIN_SAMPLE = 10         # менше оголошень — оцінка ненадійна
DISCOVERY_EXCLUDE = "broken defekt teile parts kaputt"

# (емодзі, назва, пошуковий запит, мінімальна ціна — щоб eBay не віддавав аксесуари)
CANDIDATES = [
    ("🎮", "PlayStation 5 Slim", "PlayStation 5 Slim", 250),
    ("🎮", "PlayStation 5 Pro", "PlayStation 5 Pro", 450),
    ("🎮", "Xbox Series X", "Xbox Series X", 200),
    ("🎮", "Nintendo Switch OLED", "Nintendo Switch OLED", 150),
    ("🎮", "Nintendo Switch 2", "Nintendo Switch 2", 250),
    ("🎮", "Steam Deck OLED", "Steam Deck OLED", 300),
    ("🥽", "Meta Quest 3", "Meta Quest 3", 250),
    ("📱", "iPhone 15", "iPhone 15", 350),
    ("📱", "iPhone 15 Pro", "iPhone 15 Pro", 450),
    ("📱", "iPhone 16", "iPhone 16", 450),
    ("📱", "iPhone 16 Pro", "iPhone 16 Pro", 550),
    ("📱", "Samsung Galaxy S24", "Samsung Galaxy S24", 300),
    ("🎧", "AirPods Pro 2", "AirPods Pro 2", 90),
    ("🎧", "Sony WH-1000XM5", "Sony WH-1000XM5", 120),
    ("⌚", "Apple Watch Series 9", "Apple Watch Series 9", 150),
    ("⌚", "Garmin Fenix 7", "Garmin Fenix 7", 200),
    ("💻", "MacBook Air M2", "MacBook Air M2", 500),
    ("💻", "MacBook Air M3", "MacBook Air M3", 650),
    ("📲", "iPad Air M2", "iPad Air M2", 350),
    ("🖥", "RTX 4070", "RTX 4070", 350),
    ("🚁", "DJI Mini 4 Pro", "DJI Mini 4 Pro", 400),
    ("📷", "GoPro Hero 12", "GoPro Hero 12", 150),
    ("📷", "Canon EOS R50", "Canon EOS R50", 400),
    ("🧹", "Dyson V15", "Dyson V15", 250),
]


def _already_tracked(chat_id=None):
    """Запити, які користувач уже відстежує: такі товари йому не пропонуємо."""
    return {frozenset(_search_tokens(w["query"])) for w in list_watches(chat_id=chat_id)}


def analyze_candidate(emoji, name, query, floor):
    """Один запит до eBay → метрики кандидата або None, якщо даних замало."""
    items = search_active_items(
        query=query, condition_ids=DEFAULT_CONDITION_IDS, exclude_terms=DISCOVERY_EXCLUDE,
        limit=200, min_price=floor, fresh=True,
    )
    items = [it for it in items
             if it["cond_group"] != "parts"
             and not any(is_accessory_category(n) for n in it.get("category_names") or [])]
    for it in items:
        it["spec_group"] = extract_spec_key(it["title"])

    created = [it["created_at"] for it in items if it.get("created_at")]
    update_discovery_obs(name, items, min(created) if created else None)

    # Ринок рахуємо для найпоширенішої конфігурації серед вживаних (їх перепродають найчастіше)
    used = [it for it in items if it["cond_group"] == "used"] or items
    specs = Counter(it["spec_group"] for it in used)
    spec = next((s for s, _ in specs.most_common() if s != "unspecified"), "unspecified")
    sample = [it for it in used if it["spec_group"] == spec] if specs[spec] >= DISCOVERY_MIN_SAMPLE else used
    prices = filter_outliers([it["total_price"] for it in sample])
    if len(prices) < DISCOVERY_MIN_SAMPLE:
        return None

    median = statistics.median(prices)
    sale = percentile(prices, 25)
    buy_limit = max_buy_price(sale)
    # Вигідні — дешевші за «купувати до», але не підозріло дешеві (менше половини ціни)
    raw = [it["total_price"] for it in sample]
    deals = [p for p in raw if sale * 0.5 <= p <= buy_limit]
    deal_profit = statistics.mean(estimate_resale_profit(sale, p)[1] for p in deals) if deals else 0
    deal_share = len(deals) / len(raw) if raw else 0

    gone, observed_days = discovery_gone_stats(name)
    sold_per_day = round(gone / observed_days, 1) if observed_days >= 1 else None
    new_per_day = sum(1 for c in created if c >= time.time() - 86400)
    return {
        "emoji": emoji, "name": name, "query": query, "floor": floor,
        "spec": spec if spec != "unspecified" else None,
        "sample": len(prices), "median": round(median), "sale": round(sale),
        "buy_limit": round(buy_limit), "deals_now": len(deals), "deal_profit": round(deal_profit),
        # скільки вигідних оголошень з'являється за тиждень (частка вигідних × нові за добу × 7)
        "deals_per_week": round(deal_share * new_per_day * 7, 1),
        "sold_per_day": sold_per_day, "new_per_day": new_per_day,
    }


def score(result):
    """Рейтинг: скільки можна заробити за тиждень (вигідні пропозиції × прибуток
    з кожної), з поправкою на те, як швидко товар продається."""
    weekly = result["deals_per_week"] * min(result["deal_profit"], 150)
    if result["sold_per_day"] is None:
        liquidity = 0.6  # ще не знаємо — нейтрально
    else:
        liquidity = min(1.0, 0.2 + result["sold_per_day"] / 5)
    return weekly * liquidity


def run_discovery(force=False):
    """Аналізує кандидатів, якщо минуло DISCOVERY_INTERVAL_HOURS (або force).
    Повертає кількість проаналізованих. Викликати з потоку."""
    last = float(get_meta("discovery_at", "0") or 0)
    if not force and time.time() - last < DISCOVERY_INTERVAL_HOURS * 3600:
        return 0
    done = 0
    for emoji, name, query, floor in CANDIDATES:
        if browse_budget_left() < SEARCH_RESERVE + 50:
            log.info("«Що перепродавати»: мало запитів до eBay — решту кандидатів перевірю пізніше")
            break
        try:
            result = analyze_candidate(emoji, name, query, floor)
        except Exception as e:
            log.warning("«Що перепродавати»: не вдалося проаналізувати «%s»: %s", name, e)
            continue
        if result:
            save_discovery_result(name, result)
        done += 1
    set_meta("discovery_at", time.time())
    log.info("«Що перепродавати»: проаналізовано %s товарів", done)
    return done


def top_recommendations(chat_id=None, limit=8):
    """Найкращі кандидати, яких користувач ще не відстежує, від найперспективнішого."""
    tracked = _already_tracked(chat_id)
    results = [r for r in get_discovery_results()
               if frozenset(_search_tokens(r["query"])) not in tracked and r.get("deals_now")]
    results.sort(key=score, reverse=True)
    return results[:limit]