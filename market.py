"""
Аналіз ринку: фільтри лотів, групи статистики, ціна продажу,
"купувати до", ринкове сканування.
"""

import asyncio
import statistics
from concurrent.futures import ThreadPoolExecutor

from settings import (
    ASPECT_LOOKUP_ALL,
    ASPECT_LOOKUP_ENABLED,
    ASPECT_LOOKUP_WORKERS,
    DEFAULT_CONDITION_IDS,
    EBAY_SELLING_FEES_PCT,
    MARKET_SCAN_PAGES,
    MAX_SPEC_LOOKUPS_PER_DAY,
    MAX_SPEC_LOOKUPS_PER_MARKET_SCAN,
    MIN_MODEL_SAMPLE_SIZE,
    MIN_PRICE_SUGGESTION_PCT,
    MIN_PROFIT_EUR,
    MIN_SAMPLE_SIZE,
    MIN_SOLD_SAMPLE,
    RESALE_SHIPPING_EUR,
    SALE_PRICE_PERCENTILE,
    SEARCH_RESERVE,
    TRADING_DAILY_BUDGET,
    log,
)
from laptops import SEP as LAPTOP_SEP, UNKNOWN_GPU, is_laptop, looks_like_laptop_part, laptop_spec, spec_matches, spec_parents
from laptops import needs_aspects as laptop_needs_aspects
from textparse import (
    COMPAT_ASPECTS,
    _aspect_satisfied_by_title,
    extract_spec_key,
    normalize_spec,
    console_foreign,
    console_family,
    is_accessory_category,
    plural,
    spec_key_from_aspects,
    spec_required_by_default,
)
from db import (
    delete_listing_obs_by_ids,
    get_all_listing_rows,
    get_bin_prices,
    save_bin_price,
    record_api_call,
    get_spec_rows,
    set_listing_spec,
    list_watches,
    update_watch_categories,
    mark_market_stale,
    prune_listing_obs,
    get_current_listings,
    delete_market_stats_except,
    get_api_calls_today,
    get_cached_specs,
    get_gone_prices,
    get_market_stats,
    get_rejected_ids,
    get_required_aspects,
    get_watch_categories,
    save_cached_spec,
    update_listing_observations,
    record_price_history,
    record_scan_stats,
    update_sale_price,
    upsert_market_stats,
    watch_category_ids,
)
from shared_market import MANUAL_CACHE_SECONDS, MARKET_CACHE_SECONDS, market_page, own_filter
from ebay_api import collapse_categories, trading_calls_today
from ebay_api import (
    browse_budget_left,
    fetch_item_aspects,
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


def max_buy_price(sale_price, min_profit=None):
    """
    Найвища ціна купівлі (з доставкою), за якої перепродаж ще дає щонайменше
    min_profit (за замовчуванням MIN_PROFIT_EUR) чистого прибутку:
    ціна продажу − комісія eBay − доставка покупцю − мінімальний прибуток.
    """
    net_sale = sale_price * (1 - EBAY_SELLING_FEES_PCT / 100) - RESALE_SHIPPING_EUR
    return net_sale - (MIN_PROFIT_EUR if min_profit is None else min_profit)


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
    watch_laptop = bool(watch) and is_laptop(
        query=watch.get("query") or "", category_names=[c["name"] for c in get_watch_categories(watch)])
    for it in items:
        # Ноутбуки — за класом (відеокарта · процесор · RAM або чип MacBook), решта — за пам'яттю
        it["laptop"] = watch_laptop or is_laptop(it["title"], it.get("category_names"))
        it["spec_group"] = (laptop_spec(it["title"], query=(watch or {}).get("query") or "") if it["laptop"]
                            else extract_spec_key(it["title"]))
        it["aspects"] = None

    def _spec(it, aspects):
        if it["laptop"]:
            return laptop_spec(it["title"], aspects, query=(watch or {}).get("query") or "")
        title_spec = extract_spec_key(it["title"])
        return title_spec if title_spec != "unspecified" else spec_key_from_aspects(it["title"], aspects)

    def needs_aspects(it):
        if ASPECT_LOOKUP_ALL or it["spec_group"] == "unspecified":
            return True
        if it["laptop"] and laptop_needs_aspects(it["spec_group"]):
            return True
        return any(not _aspect_satisfied_by_title(name, it["title"]) for name in required)

    candidates = [it for it in items if it.get("item_id") and needs_aspects(it)]
    if not candidates:
        return items
    # Першими — ноутбуки з невідомою відеокартою: без неї їх не оцінити
    candidates.sort(key=lambda it: not (it["laptop"] and laptop_needs_aspects(it["spec_group"])))
    cached = get_cached_specs([it["item_id"] for it in candidates])

    lookups_left = 0
    if ASPECT_LOOKUP_ENABLED and max_lookups > 0:
        lookups_left = min(
            max_lookups,
            MAX_SPEC_LOOKUPS_PER_DAY - get_api_calls_today("item"),
            browse_budget_left() - SEARCH_RESERVE,  # характеристики — лише з "вільного" бюджету
        )

    to_fetch = []
    for it in candidates:
        entry = cached.get(it["item_id"])
        if entry is not None:
            spec, aspects = entry
            if it["laptop"]:
                if aspects is not None:   # клас ноутбука — завжди заново з назви й характеристик
                    it["spec_group"] = _spec(it, aspects)
                    it["aspects"] = aspects
                    continue
            elif it["spec_group"] == "unspecified":
                it["spec_group"] = normalize_spec(it["title"], spec)   # кеш міг бути в старому форматі
            if aspects is not None:
                it["aspects"] = aspects
                continue
            if not required and not ASPECT_LOOKUP_ALL:
                continue  # старий запис кешу без характеристик — для конфігурації достатньо
        if lookups_left <= 0:
            continue
        lookups_left -= 1
        to_fetch.append(it)

    if not to_fetch:
        return items

    def _fetch(item_id):
        try:
            return fetch_item_aspects(item_id)
        except Exception as e:
            log.debug("Не вдалося отримати характеристики лота %s: %s", item_id, e)
            return None

    # Запити паралельно — інакше 40 лотів це ~20 секунд очікування
    with ThreadPoolExecutor(max_workers=min(ASPECT_LOOKUP_WORKERS, len(to_fetch))) as pool:
        results = list(pool.map(_fetch, [it["item_id"] for it in to_fetch]))

    for it, aspects in zip(to_fetch, results):
        if aspects is None:
            continue
        spec = _spec(it, aspects)
        save_cached_spec(it["item_id"], spec, aspects)
        it["spec_group"] = spec
        it["aspects"] = aspects
    return items


def watch_requires_spec(w):
    value = w.get("require_spec")
    if value is None:
        # Ноутбук: клас визначається й без пам'яті в назві (з характеристик) — не відкидаємо
        if is_laptop(query=w.get("query") or "", category_names=[c["name"] for c in get_watch_categories(w)]):
            return False
        # PS4/PS5: пам'ять, якої в моделі не буває («64 GB» у характеристиках), — помилка продавця;
        # оголошення лишається в «конфігурація невідома», а не відкидається
        if console_family(w.get("query") or ""):
            return False
        return spec_required_by_default(w["query"])
    return bool(value)


MAX_BIN_LOOKUPS = 20   # ціни «купити зараз» через Trading API за один виклик фільтрів


def _buy_it_now_prices(items):
    """«Аукціон + купити зараз», де пошук віддав як ціну ставку: ціна «купити зараз» —
    з кешу або через Trading API (окремий ліміт, лише з входом в акаунт). {item_id: ціна}."""
    if not items:
        return {}
    prices = get_bin_prices([it["item_id"] for it in items])
    missing = [it["item_id"] for it in items if it["item_id"] not in prices][:MAX_BIN_LOOKUPS]
    if not missing:
        return prices
    from ebay_user import is_connected   # тут, щоб не було циклу імпортів
    from trading_api import fetch_buy_it_now
    if not is_connected():
        return prices
    for item_id in missing:
        if trading_calls_today()[0] >= TRADING_DAILY_BUDGET:
            break
        record_api_call("trading:watch")
        try:
            price = fetch_buy_it_now(item_id)
        except Exception as e:
            log.debug("Не вдалося дізнатися ціну «купити зараз» для %s: %s", item_id, e)
            continue
        if price:
            save_bin_price(item_id, price)
            prices[item_id] = price
    return prices


def _use_buy_it_now(it, bin_prices):
    """Ціна оголошення = «купити зараз» (аукціон — лише бонус). False — ціна невідома."""
    price = bin_prices.get(it["item_id"])
    if not price:
        return False
    delta = price - (it.get("current_bid") or it["price"])
    ratio = it["effective_price"] / it["total_price"] if it.get("total_price") else 1
    it["price"] = price
    it["total_price"] += delta
    it["effective_price"] = it["total_price"] * ratio
    it["bid_is_price"] = False
    return True


def _apply_item_filters(w, items):
    """
    Жорсткі фільтри лотів:
      1. У характеристиках є "Kompatible Marke/Modell" → аксесуар, відкидаємо
         (для будь-якого товару, якщо характеристики лота завантажені).
      2. Обрані обов'язкові характеристики → лот без будь-якої з них відкидаємо; лот,
         характеристики якого ще не завантажені, відкладаємо до наступного циклу.
      3. Інакше, якщо ввімкнено "лише з відомою пам'яттю" → без пам'яті відкидаємо.
      0. Лоти, позначені користувачем «🚫 Не той товар», відкидаються завжди.
    """
    required = get_required_aspects(w)
    rejected = get_rejected_ids(w["id"]) if w.get("id") else set()
    # Якщо товар шукається у великій категорії («Handys & Kommunikation»), туди
    # входять і аксесуари. Оголошення, яке сам продавець поклав у категорію
    # чохлів/запчастин, — не товар (хіба що користувач сам обрав таку категорію).
    wants_accessories = any(is_accessory_category(c["name"]) for c in get_watch_categories(w))
    laptop_watch = is_laptop(query=w.get("query") or "", category_names=[c["name"] for c in get_watch_categories(w)])
    bin_prices = _buy_it_now_prices([it for it in items if it.get("bid_is_price") and it.get("item_id") not in rejected])
    kept = []
    for it in items:
        if it.get("item_id") in rejected:
            continue
        if console_foreign(it["title"], w.get("query") or ""):
            continue   # у товарі «PS4» — PS5 (825GB) чи PS3 (320GB)
        if it.get("bid_is_price") and not _use_buy_it_now(it, bin_prices):
            continue   # ціна «купити зараз» невідома, а ставка ще зросте — не порівнюємо
        # Ноутбук: «RAM passend für ROG Strix G15» — запчастина, хоч у назві й модель ноутбука
        if laptop_watch and looks_like_laptop_part(it["title"], it.get("category_names")):
            continue
        if not wants_accessories and any(is_accessory_category(n) for n in it.get("category_names") or []):
            continue
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


def laptop_unknown(it):
    """Ноутбук, клас якого ще не відомий (немає відеокарти) — оцінювати рано."""
    return bool(it.get("laptop")) and laptop_needs_aspects(it.get("spec_group") or "unspecified")


def _stat_for_item(stats, it):
    """Спершу статистика точної конфігурації; якщо окремої немає (замало
    оголошень) — найближчий ширший клас ноутбука, і лише тоді загальна група стану."""
    cond, spec = it["cond_group"], it.get("spec_group") or "unspecified"
    for key in [spec] + spec_parents(spec):
        if (cond, key) in stats:
            return stats[(cond, key)]
    return stats.get((cond, "*"))


def _fetch_market_items(w, max_age=MARKET_CACHE_SECONDS):
    """
    До MARKET_SCAN_PAGES × 200 найновіших оголошень у КОЖНІЙ категорії товару.
    Повертає (відфільтровані лоти, вікно для трекера, id усіх знайдених лотів).
    Вікно — найпізніша з "найстаріших дат" по категоріях: лот, створений після
    неї, гарантовано мав би потрапити у видачу своєї категорії.
    """
    seen_ids, items, window_starts = set(), [], []
    for cid in (watch_category_ids(w) or [None]):
        cat_created = []
        for page in range(MARKET_SCAN_PAGES):
            # Спільна сторінка: той самий товар інших користувачів eBay повторно не сканує
            batch = market_page(w, cid, page * 200, max_age=max_age)
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
    items = own_filter(w, items)   # особисті фільтри (стан, мін. ціна, виключені слова)
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
        if it.get("auction"):
            continue   # «аукціон + купити зараз»: у списках для купівлі є, у статистиці — ні
        groups.setdefault((it["cond_group"], "*"), []).append(it["total_price"])
        # Ноутбук з невідомою відеокартою — лише в «усі конфігурації»: група «GPU ?» — суміш усього
        if it["spec_group"] != "unspecified" and not it["spec_group"].startswith(UNKNOWN_GPU):
            groups.setdefault((it["cond_group"], it["spec_group"]), []).append(it["total_price"])
            # Ноутбуки: ще й ширші класи («RTX 4060 · i7 13 gen» → «RTX 4060»), якщо в точному мало
            for parent in spec_parents(it["spec_group"]):
                groups.setdefault((it["cond_group"], parent), []).append(it["total_price"])

    stats = {}
    for (cond, spec), prices in groups.items():
        # Конфігурація, що містить УСІ лоти цього стану, дублювала б групу "*"
        if spec != "*" and len(prices) == len(groups[(cond, "*")]):
            continue
        # Ширший клас, що збігається з одним вужчим, — зайвий дублікат
        if spec != "*" and any(c == cond and s.startswith(spec + LAPTOP_SEP) and len(p) == len(prices)
                               for (c, s), p in groups.items()):
            continue
        clean = filter_outliers(prices)
        if len(clean) < MIN_SAMPLE_SIZE:
            continue
        median_price = statistics.median(clean)
        gone = filter_outliers(get_gone_prices(watch_id, cond, None if spec == "*" else spec))
        if len(gone) >= MIN_SOLD_SAMPLE:
            sale_price = min(statistics.median(gone), median_price)
            source = f"за {plural(len(gone), 'проданим', 'проданими', 'проданими')}"
        else:
            sale_price = percentile(clean, SALE_PRICE_PERCENTILE)
            source = "за поточними оголошеннями"
        stats[(cond, spec)] = {
            "cond_group": cond, "spec_group": spec,
            "median_price": median_price, "sale_price": sale_price,
            "sale_source": source, "sample_size": len(clean),
        }
    return stats


def prune_laptop_parts():
    """Раз на добу: з історії ноутбуків прибрати запчастини, що потрапили туди раніше
    (до появи фільтра) — інакше вони занижують ціни. Повертає кількість прибраних."""
    removed = 0
    for w in list_watches(active_only=True):
        if not is_laptop(query=w["query"], category_names=[c["name"] for c in get_watch_categories(w)]):
            continue
        ids = [r["item_id"] for r in get_all_listing_rows(w["id"]) if looks_like_laptop_part(r.get("title") or "")]
        if ids:
            delete_listing_obs_by_ids(w["id"], ids)
            mark_market_stale(w["id"])
            removed += len(ids)
            log.info("«%s»: прибрано з історії запчастин/аксесуарів: %s", w["label"], len(ids))
    return removed


def normalize_saved_specs():
    """Історія й кеш у новому форматі конфігурацій (iPhone — лише пам'ять: «128GB+8GB» → «128GB»;
    PS4/PS5 — лише справжні об'єми; 1000GB → 1TB) і без чужих моделей консолей (PS5 у товарі PS4).
    Раз на добу і після запуску; змінені товари перераховуються. Повертає кількість змінених записів."""
    queries = {w["id"]: w["query"] for w in list_watches()}
    changed_watches, changed, foreign = set(), 0, {}
    for r in get_spec_rows(None):
        if console_foreign(r["title"] or "", queries.get(r["watch_id"], "")):
            foreign.setdefault(r["watch_id"], []).append(r["item_id"])
            continue
        new = normalize_spec(r["title"], r["spec_group"])
        if new == r["spec_group"]:
            continue
        set_listing_spec(r["watch_id"], r["item_id"], new)
        changed_watches.add(r["watch_id"])
        changed += 1
    for wid, ids in foreign.items():
        delete_listing_obs_by_ids(wid, ids)
        changed_watches.add(wid)
        changed += len(ids)
    for w in changed_watches:
        mark_market_stale(w)
    if changed:
        log.info("Конфігурації приведено до нового формату / прибрано чужі моделі: %s записів", changed)
    return changed


def collapse_watch_categories():
    """Раз на добу: якщо в товару вибрані і батьківська, і вкладена категорія
    (Computer, Tablets & Netzwerk + Notebooks), лишаємо лише батьківську — історія не змінюється.
    Викликати з потоку. Повертає кількість змінених товарів."""
    changed = 0
    for w in list_watches(active_only=True):
        cats = get_watch_categories(w)
        if len(cats) < 2:
            continue
        kept = collapse_categories(cats)
        if len(kept) < len(cats):
            update_watch_categories(w["id"], w["chat_id"], kept)
            changed += 1
            log.info("«%s»: прибрано вкладені категорії, лишились %s", w["label"],
                     ", ".join(c["name"] for c in kept))
    return changed


def apply_filters_to_history(w):
    """Після зміни мінімальної ціни чи характеристик: не стираємо історію, а прибираємо з неї
    лише те, що не проходить нові фільтри; ринок перерахується найближчим циклом."""
    removed = prune_listing_obs(w["id"], min_price=w.get("min_price") or 0,
                                drop_unspecified=watch_requires_spec(w))
    mark_market_stale(w["id"])
    refresh_sale_prices(w["id"])
    if removed:
        log.info("watch #%s: нові фільтри — прибрано з історії %s оголошень", w["id"], removed)
    return removed


def refresh_sale_prices(watch_id):
    """Перераховує «продати за» з уже збережених даних (без запитів до eBay) —
    напр. після того, як користувач прибрав чужий лот зі статистики продажів."""
    active = {}
    for r in get_current_listings(watch_id):
        if "AUCTION" in (r.get("buying_options") or ""):
            continue
        active.setdefault(r["cond_group"], []).append(r)
    for s in get_market_stats(watch_id):
        cond, spec = s["cond_group"], s["spec_group"]
        gone = filter_outliers(get_gone_prices(watch_id, cond, None if spec == "*" else spec))
        if len(gone) >= MIN_SOLD_SAMPLE:
            sale = min(statistics.median(gone), s["median_price"])
            source = f"за {plural(len(gone), 'проданим', 'проданими', 'проданими')}"
        elif "продан" in (s["sale_source"] or ""):
            prices = filter_outliers([r["price"] for r in active.get(cond, [])
                                      if spec_matches(r["spec_group"], spec)])
            if len(prices) < MIN_SAMPLE_SIZE:
                continue
            sale, source = percentile(prices, SALE_PRICE_PERCENTILE), "за поточними оголошеннями"
        else:
            continue
        update_sale_price(watch_id, cond, spec, sale, source)


async def _recalculate_watch_medians(w: dict, replace_existing=False):
    """
    Ринкове сканування: велика вибірка найновіших лотів → спостереження
    за лотами (трекер зниклих) → статистика груп. Повертає
    (items, stats, newly_computed_keys).
    """
    max_age = MANUAL_CACHE_SECONDS if replace_existing else MARKET_CACHE_SECONDS
    items, window_start, present_ids = await asyncio.to_thread(_fetch_market_items, w, max_age)
    if not items:
        return [], {}, []

    new_count = await asyncio.to_thread(update_listing_observations, w["id"], items, window_start, present_ids)
    record_scan_stats(w["id"], len(present_ids or ()), len(items), new_count or 0)
    existing_keys = {(s["cond_group"], s["spec_group"]) for s in get_market_stats(w["id"])}
    stats = _compute_group_stats(w["id"], items)

    delete_market_stats_except(w["id"], set(stats))
    for (cond, spec), s in stats.items():
        upsert_market_stats(w["id"], cond, spec, s["median_price"], s["sample_size"],
                            s["sale_price"], s["sale_source"])
    record_price_history(w["id"], stats.values())
    newly = [key for key in stats if key not in existing_keys]
    return items, stats, newly
