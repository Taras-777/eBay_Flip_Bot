"""
Аналіз ринку: фільтри лотів, групи статистики, ціна продажу,
"купувати до", ринкове сканування.
"""

import asyncio
import statistics

from settings import (
    ASPECT_LOOKUP_ALL,
    ASPECT_LOOKUP_ENABLED,
    DEFAULT_CONDITION_IDS,
    EBAY_SELLING_FEES_PCT,
    MARKET_SCAN_PAGES,
    MAX_ALLOWED_THRESHOLD_PCT,
    MAX_SPEC_LOOKUPS_PER_DAY,
    MAX_SPEC_LOOKUPS_PER_MARKET_SCAN,
    MIN_ALLOWED_THRESHOLD_PCT,
    MIN_MODEL_SAMPLE_SIZE,
    MIN_PRICE_SUGGESTION_PCT,
    MIN_PROFIT_EUR,
    MIN_SAMPLE_SIZE,
    MIN_SOLD_SAMPLE,
    RESALE_SHIPPING_EUR,
    SALE_PRICE_PERCENTILE,
    SEARCH_RESERVE,
    log,
)
from textparse import (
    COMPAT_ASPECTS,
    _aspect_satisfied_by_title,
    extract_spec_key,
    spec_key_from_aspects,
    spec_required_by_default,
)
from db import (
    delete_market_stats_except,
    get_api_calls_today,
    get_cached_specs,
    get_gone_prices,
    get_market_stats,
    get_required_aspects,
    save_cached_spec,
    update_listing_observations,
    upsert_market_stats,
    watch_category_ids,
)
from ebay_api import (
    _watch_search_kwargs,
    browse_budget_left,
    fetch_item_aspects,
    search_active_items,
    search_in_categories,
)


# ============================================================
# АНАЛІЗ ЦІН
# ============================================================

def compute_median(prices, min_sample_size=MIN_SAMPLE_SIZE):
    if len(prices) < min_sample_size:
        return None
    return statistics.median(prices)


def minimum_sample_size_for_query(query):
    """Мінімум оголошень у групі для надійної медіани. Менші групи не
    рахуються окремо — їхні лоти порівнюються із загальною групою стану."""
    return MIN_SAMPLE_SIZE


def percentile(values, pct):
    """Лінійна інтерполяція між сусідніми значеннями відсортованого списку."""
    ordered = sorted(values)
    if not ordered:
        return None
    k = (len(ordered) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def target_profit(sale_price, target_pct):
    """Бажаний прибуток: target_pct% від ціни продажу, але не менше MIN_PROFIT_EUR."""
    return max(sale_price * target_pct / 100, MIN_PROFIT_EUR)


def max_buy_price(sale_price, target_pct):
    """
    Найвища ціна купівлі (з доставкою), за якої перепродаж ще дає бажаний
    прибуток: ціна продажу − комісія eBay − доставка покупцю − прибуток.
    """
    net_sale = sale_price * (1 - EBAY_SELLING_FEES_PCT / 100) - RESALE_SHIPPING_EUR
    return net_sale - target_profit(sale_price, target_pct)


def estimate_resale_profit(sale_price, purchase_price):
    """
    Прибуток при перепродажу за sale_price після комісії eBay і доставки.
    ВАЖЛИВО: sale_price — це ОЦІНКА (ціни зниклих лотів або нижня частина
    активних пропозицій), а не підтверджена ціна продажу: eBay Browse API
    не показує завершені продажі. Повертає (sale_price, profit).
    """
    net_sale = sale_price * (1 - EBAY_SELLING_FEES_PCT / 100) - RESALE_SHIPPING_EUR
    return sale_price, net_sale - purchase_price


def filter_outliers(prices):
    if len(prices) < MIN_SAMPLE_SIZE:
        return prices
    sorted_p = sorted(prices)
    q1 = statistics.median(sorted_p[: len(sorted_p) // 2])
    q3 = statistics.median(sorted_p[(len(sorted_p) + 1) // 2:])
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    return [p for p in prices if lower <= p <= upper]


def suggest_min_price(query, category_ids=None, condition_ids=DEFAULT_CONDITION_IDS):
    """
    Рахує медіану в обраній категорії і пропонує мінімальну ціну як
    MIN_PRICE_SUGGESTION_PCT% від неї (округлено до 5€). Повертає
    (min_price, median, sample_size) або None, якщо даних замало.
    """
    try:
        items = search_in_categories(
            category_ids or [], query=query, condition_ids=condition_ids, limit=100,
        )
    except Exception as e:
        log.warning("Не вдалося оцінити мінімальну ціну для «%s»: %s", query, e)
        return None
    prices = filter_outliers([it["total_price"] for it in items])
    if len(prices) < MIN_MODEL_SAMPLE_SIZE:
        return None
    median_price = statistics.median(prices)
    suggestion = max(5, round(median_price * MIN_PRICE_SUGGESTION_PCT / 100 / 5) * 5)
    return suggestion, median_price, len(prices)


def analyze_market_for_threshold(query: str, category_ids=None, min_price=None):
    """
    Реальний аналіз ринку: робить запит до eBay Browse API за назвою
    товару (в обраній категорії та від мінімальної ціни), бере поточні
    активні оголошення і рахує коефіцієнт варіації цін (розкид
    відносно медіани). Що більший розкид — то вищий поріг знижки треба
    ставити, інакше цілком нормальна ціна для варіативного товару буде
    помилково сприйматись як "вигідна".

    Повертає dict {pct, reason, median, sample_size} на основі
    РЕАЛЬНИХ поточних даних, або None, якщо eBay недоступний чи лотів
    замало для надійного розрахунку.
    """
    try:
        items = search_in_categories(
            category_ids or [], query=query, limit=100, min_price=min_price,
        )
    except Exception as e:
        log.warning("Не вдалося проаналізувати ринок для «%s»: %s", query, e)
        return None

    if not items:
        return None

    prices = [it["total_price"] for it in items]
    clean = filter_outliers(prices)
    if len(clean) < MIN_SAMPLE_SIZE:
        return None

    median_price = statistics.median(clean)
    stdev = statistics.pstdev(clean)
    cv = (stdev / median_price) if median_price else 0  # коефіцієнт варіації

    if cv < 0.15:
        pct, desc = 15, "дуже стабільний ринок, ціни майже однакові"
    elif cv < 0.25:
        pct, desc = 20, "стандартизований товар, невеликий розкид цін"
    elif cv < 0.35:
        pct, desc = 25, "помірний розкид цін"
    elif cv < 0.50:
        pct, desc = 32, "великий розкид цін"
    else:
        pct, desc = 40, "дуже великий розкид цін, товар неоднорідний"

    pct = max(MIN_ALLOWED_THRESHOLD_PCT, min(MAX_ALLOWED_THRESHOLD_PCT, pct))
    reason = (
        f"на основі {len(clean)} поточних оголошень з eBay: медіана "
        f"{median_price:.0f}€, розкид цін ~{cv * 100:.0f}% ({desc})"
    )

    spec_warning = _detect_spec_price_mismatch(items)
    return {
        "pct": pct, "reason": reason, "median": median_price,
        "sample_size": len(clean), "spec_warning": spec_warning,
    }


def _detect_spec_price_mismatch(items):
    """
    Перевіряє, чи в межах одного запиту трапляються різні виявлені
    конфігурації (обсяг пам'яті/накопичувача) з помітно різними
    медіанними цінами — типовий приклад: "iPhone 12" без зазначення
    пам'яті змішує 64/128/256GB. Якщо так — повертає текст-підказку
    вказати конкретну конфігурацію в назві для точнішого відстеження.
    """
    by_spec = {}
    for it in items:
        spec = extract_spec_key(it["title"])
        if spec == "unspecified":
            continue
        by_spec.setdefault(spec, []).append(it["total_price"])

    candidates = {spec: statistics.median(prices) for spec, prices in by_spec.items() if len(prices) >= 3}
    if len(candidates) < 2:
        return None

    lowest_spec = min(candidates, key=candidates.get)
    highest_spec = max(candidates, key=candidates.get)
    low, high = candidates[lowest_spec], candidates[highest_spec]
    if low <= 0 or (high - low) / low < 0.15:
        return None  # різниця незначна, окреме уточнення не критичне

    parts = ", ".join(f"{spec} ~{price:.0f}€" for spec, price in sorted(candidates.items(), key=lambda kv: kv[1]))
    return (
        f"у результатах трапляються різні конфігурації з різними цінами ({parts}). "
        f"Бот і так рахує медіану окремо для кожної виявленої конфігурації, але для "
        f"точнішого відстеження краще вказати конкретний обсяг у назві (напр. «256GB»)."
    )


def _annotate_items(items, max_lookups=0, watch=None):
    """
    Визначає конфігурацію кожного лота і, де потрібно, завантажує його
    характеристики (it["aspects"]; None — не завантажені).

    Характеристики потрібні, якщо: у назві немає пам'яті/процесора, або для
    товару обрана обов'язкова характеристика, якої не видно з назви.
    Джерело — кеш, інакше getItem (не більше max_lookups за виклик і
    MAX_SPEC_LOOKUPS_PER_DAY за добу). Невстиглі лоти перевіряться наступного
    разу. Мережеві запити — лише з потоку (asyncio.to_thread).
    """
    required = get_required_aspects(watch)
    for it in items:
        it["spec_group"] = extract_spec_key(it["title"])
        it["aspects"] = None

    def needs_aspects(it):
        if ASPECT_LOOKUP_ALL or it["spec_group"] == "unspecified":
            return True
        return any(not _aspect_satisfied_by_title(name, it["title"]) for name in required)

    candidates = [it for it in items if it.get("item_id") and needs_aspects(it)]
    if not candidates:
        return items
    cached = get_cached_specs([it["item_id"] for it in candidates])

    lookups_left = 0
    if ASPECT_LOOKUP_ENABLED and max_lookups > 0:
        lookups_left = min(
            max_lookups,
            MAX_SPEC_LOOKUPS_PER_DAY - get_api_calls_today("item"),
            browse_budget_left() - SEARCH_RESERVE,  # характеристики — лише з "вільного" бюджету
        )

    for it in candidates:
        title_spec = it["spec_group"]
        entry = cached.get(it["item_id"])
        if entry is not None:
            spec, aspects = entry
            if title_spec == "unspecified":
                it["spec_group"] = spec
            if aspects is not None:
                it["aspects"] = aspects
                continue
            if not required and not ASPECT_LOOKUP_ALL:
                continue  # старий запис кешу без характеристик — для конфігурації достатньо
        if lookups_left <= 0:
            continue
        lookups_left -= 1
        try:
            aspects = fetch_item_aspects(it["item_id"])
        except Exception as e:
            log.debug("Не вдалося отримати характеристики лота %s: %s", it["item_id"], e)
            continue
        spec = title_spec if title_spec != "unspecified" else spec_key_from_aspects(it["title"], aspects)
        save_cached_spec(it["item_id"], spec, aspects)
        it["spec_group"] = spec
        it["aspects"] = aspects
    return items


def watch_requires_spec(w):
    value = w.get("require_spec")
    if value is None:
        return spec_required_by_default(w["query"])
    return bool(value)


def _apply_item_filters(w, items):
    """
    Жорсткі фільтри лотів:
      1. У характеристиках є "Kompatible Marke/Modell" → аксесуар, відкидаємо
         (для будь-якого товару, якщо характеристики лота завантажені).
      2. Обрані обов'язкові характеристики → лот без будь-якої з них відкидаємо; лот,
         характеристики якого ще не завантажені, відкладаємо до наступного циклу.
      3. Інакше, якщо ввімкнено "лише з відомою пам'яттю" → без пам'яті відкидаємо.
    """
    required = get_required_aspects(w)
    kept = []
    for it in items:
        aspects = it.get("aspects")
        if aspects and any(name in COMPAT_ASPECTS for name in aspects):
            continue
        if required:
            # Потрібні ВСІ обрані характеристики
            missing = [name for name in required if not _aspect_satisfied_by_title(name, it["title"])]
            if missing:
                if aspects is None:
                    continue  # ще не перевірений — наступного циклу
                if any(not str(aspects.get(name.lower()) or "").strip() for name in missing):
                    continue
        elif watch_requires_spec(w) and it.get("spec_group") == "unspecified":
            continue
        kept.append(it)
    return kept


def _stat_for_item(stats, it):
    """Спершу статистика точної конфігурації; якщо окремої немає (замало
    оголошень) — загальна група цього стану."""
    return stats.get((it["cond_group"], it["spec_group"])) or stats.get((it["cond_group"], "*"))


def _fetch_market_items(w):
    """
    До MARKET_SCAN_PAGES × 100 найновіших оголошень у КОЖНІЙ категорії товару.
    Повертає (відфільтровані лоти, вікно для трекера, id усіх знайдених лотів).
    Вікно — найпізніша з "найстаріших дат" по категоріях: лот, створений після
    неї, гарантовано мав би потрапити у видачу своєї категорії.
    """
    seen_ids, items, window_starts = set(), [], []
    for cid in (watch_category_ids(w) or [None]):
        cat_created = []
        for page in range(MARKET_SCAN_PAGES):
            batch = search_active_items(
                limit=100, offset=page * 100, fresh=True, category_id=cid, **_watch_search_kwargs(w),
            )
            if not batch:
                break
            for it in batch:
                if it.get("created_at"):
                    cat_created.append(it["created_at"])
                if it["item_id"] and it["item_id"] not in seen_ids:
                    seen_ids.add(it["item_id"])
                    items.append(it)
        if cat_created:
            window_starts.append(min(cat_created))
    window_start = max(window_starts) if window_starts else None
    _annotate_items(items, max_lookups=MAX_SPEC_LOOKUPS_PER_MARKET_SCAN, watch=w)
    return _apply_item_filters(w, items), window_start, seen_ids


def _compute_group_stats(watch_id, items):
    """
    Групи статистики:
      (стан, "*")            — усі лоти цього стану; є завжди, якщо лотів ≥ MIN_SAMPLE_SIZE;
      (стан, конфігурація)   — окремо, лише якщо в конфігурації ≥ MIN_SAMPLE_SIZE лотів.
    Так та сама консоль не розсипається на дрібні групи з 3–4 оголошень.

    Для кожної групи:
      median_price — медіана цін пропозиції (для довідки й тренду);
      sale_price   — реалістична ціна продажу: медіана цін "зниклих" лотів,
                     якщо їх назбиралось ≥ MIN_SOLD_SAMPLE (не вище медіани
                     пропозицій — зняті продавцями дорогі лоти можуть її завищувати),
                     інакше SALE_PRICE_PERCENTILE-й перцентиль пропозицій.
    """
    groups = {}
    for it in items:
        groups.setdefault((it["cond_group"], "*"), []).append(it["total_price"])
        if it["spec_group"] != "unspecified":
            groups.setdefault((it["cond_group"], it["spec_group"]), []).append(it["total_price"])

    stats = {}
    for (cond, spec), prices in groups.items():
        # Конфігурація, що містить УСІ лоти цього стану, дублювала б групу "*"
        if spec != "*" and len(prices) == len(groups[(cond, "*")]):
            continue
        clean = filter_outliers(prices)
        if len(clean) < MIN_SAMPLE_SIZE:
            continue
        median_price = statistics.median(clean)
        gone = filter_outliers(get_gone_prices(watch_id, cond, None if spec == "*" else spec))
        if len(gone) >= MIN_SOLD_SAMPLE:
            sale_price = min(statistics.median(gone), median_price)
            source = f"зниклі лоти, {len(gone)} шт."
        else:
            sale_price = percentile(clean, SALE_PRICE_PERCENTILE)
            source = "нижня чверть пропозицій"
        stats[(cond, spec)] = {
            "cond_group": cond, "spec_group": spec,
            "median_price": median_price, "sale_price": sale_price,
            "sale_source": source, "sample_size": len(clean),
        }
    return stats


async def _recalculate_watch_medians(w: dict, replace_existing=False):
    """
    Ринкове сканування: велика вибірка найновіших лотів → спостереження
    за лотами (трекер зниклих) → статистика груп. Повертає
    (items, stats, newly_computed_keys).
    """
    items, window_start, present_ids = await asyncio.to_thread(_fetch_market_items, w)
    if not items:
        return [], {}, []

    await asyncio.to_thread(update_listing_observations, w["id"], items, window_start, present_ids)
    existing_keys = {(s["cond_group"], s["spec_group"]) for s in get_market_stats(w["id"])}
    stats = _compute_group_stats(w["id"], items)

    delete_market_stats_except(w["id"], set(stats))
    for (cond, spec), s in stats.items():
        upsert_market_stats(w["id"], cond, spec, s["median_price"], s["sample_size"],
                            s["sale_price"], s["sale_source"])
    newly = [key for key in stats if key not in existing_keys]
    return items, stats, newly