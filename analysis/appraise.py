"""
«💶 Оцінити товар»: за скільки можна продати товар (за посиланням на оголошення або за назвою).

Звідки ціна (eBay не дає звичайним розробникам пошуку проданих — Marketplace Insights API лише для
обраних партнерів), тож бот бере те, що вже зібрав сам:
  1. продажі за SOLD_LOOKBACK_DAYS днів з усіх товарів бота з тим самим товаром у назві (твої, інших
     користувачів, з архіву товарів), з «💡 Що перепродавати» і з раніше оцінених товарів — того самого
     класу / конфігурації, 👤 приватні;
  2. якщо їх замало — разом зі старішими продажами (до APPRAISE_OLD_DAYS днів, з «🗃 Архіву продажів»):
     свіжі важать більше, текст чесно каже, що ціни могли змінитися;
  3. якщо продажів замало і так — поточні оголошення 👤 приватних того самого класу (з бази, а якщо й там
     мало — разовий пошук на eBay), нижня чверть цін — як у звичайних знахідках.
Якщо оцінка за оголошеннями, бот пропонує «👀 Спостерігати 7 днів»: тихий товар, що збирає продажі.
Товар, якого немає ні серед товарів бота, ні в «💡», бот запам'ятовує і раз на добу збирає по ньому
продажі (поки є запас запитів) — наступна оцінка буде вже за ними.
"""

import re
import statistics
import time

from core.settings import DEFAULT_CONDITION_IDS, LISTING_OBS_RETENTION_DAYS, MIN_SOLD_SAMPLE, SOLD_LOOKBACK_DAYS, log
from core.db import (
    APPRAISED_PREFIX,
    add_appraised_product,
    archived_queries,
    get_archived_sales,
    list_appraised_products,
    get_cached_specs,
    get_category_profits,
    get_current_listings,
    get_discovery_sold,
    get_min_profit,
    get_sold_listings,
    list_archived_watches,
    list_watches,
    profit_needed,
)
from analysis.laptops import (
    SEP,
    UNKNOWN_GPU,
    is_laptop,
    laptop_spec,
    price_fallbacks,
    scoped_label,
    spec_kind,
    spec_matches,
)
from analysis.market import (
    RESALE_GROUP,
    auto_watch_group,
    estimate_resale_profit,
    filter_outliers,
    max_buy_price,
    percentile,
    recent_median,
    typical_range,
)
from analysis.sales import summarize
from analysis.textparse import (
    CURRENCY_TO_EUR,
    _search_tokens,
    _title_matches_search,
    extract_spec_key,
    market_group,
    spec_key_from_aspects,
)

APPRAISE_LISTINGS_MIN = 5        # оголошень, щоб оцінити ціну за ними
APPRAISE_SEARCH_LIMIT = 200      # разовий пошук на eBay (≈1 запит)
APPRAISE_OLD_DAYS = 180          # старіші продажі (2–6 місяців) — запасний варіант, коли свіжих замало
APPRAISE_FLOOR_SHARE = 0.4       # мін. ціна пошуку для оціненого товару — частка оцінки (без аксесуарів)
EXCLUDE_TERMS = "broken defekt teile parts kaputt"
# Слова з назви оголошення, які не описують сам товар — для запиту разового пошуку
_NOISE = {"neu", "neuwertig", "ovp", "top", "zustand", "wie", "sehr", "gut", "gebraucht", "mit", "und", "inkl",
          "garantie", "rechnung", "original", "versand", "kostenlos", "used", "new", "the", "for", "und", "in",
          "grau", "schwarz", "silber", "space", "gray", "grey", "black", "silver", "weiß", "weiss", "blau"}


class AppraiseError(Exception):
    """Зрозуміла причина, чому оцінити не вдалося."""


# ---------- що оцінюємо ----------

def read_listing(text):
    """Оголошення з посилання чи номера: {title, url, total, aspects, categories, cond_group} або None —
    у тексті немає посилання (тоді це назва товару). Мережеві запити — викликати з потоку."""
    from ebay.ebay_api import fetch_item_by_legacy_id, resolve_item_id   # тут: мережевий модуль лише за потреби
    if not re.search(r"https?://|\b\d{10,15}\b", text or ""):
        return None
    legacy_id = resolve_item_id(text)
    if not legacy_id:
        raise AppraiseError("Не бачу в посиланні номера оголошення eBay.")
    item = fetch_item_by_legacy_id(legacy_id)
    if item is None:
        raise AppraiseError("eBay не знайшов такого оголошення — можливо, його вже видалили.")
    try:
        price = float((item.get("price") or {}).get("value"))
    except (TypeError, ValueError):
        price = None
    rate = CURRENCY_TO_EUR.get(((item.get("price") or {}).get("currency") or "EUR").upper())
    costs = []
    for opt in item.get("shippingOptions") or []:
        try:
            costs.append(float((opt.get("shippingCost") or {}).get("value", 0)))
        except (TypeError, ValueError):
            pass
    total = round((price + (min(costs) if costs else 0.0)) * rate, 2) if price is not None and rate else None
    aspects = {(a.get("name") or "").strip().lower(): a.get("value") or "" for a in item.get("localizedAspects") or []}
    return {"title": item.get("title") or "", "url": item.get("itemWebUrl") or f"https://www.ebay.de/itm/{legacy_id}",
            "total": total, "aspects": aspects, "cond_group": market_group(item),
            "categories": [c for c in (item.get("categoryPath") or "").split("|") if c]}


def classify(title, aspects=None, categories=(), query=""):
    """(клас / конфігурація, ноутбук?) — так само, як для оголошень товарів."""
    aspects = aspects or {}
    if is_laptop(title, categories, query):
        return laptop_spec(title, aspects, query=query), True
    spec = extract_spec_key(title)
    if spec == "unspecified":
        spec = spec_key_from_aspects(title, aspects)
    return spec, False


def search_query(title):
    """Запит для разового пошуку з назви: без кольорів, «OVP», «Top Zustand» тощо, до 6 слів."""
    words = [w for w in re.split(r"[\s|,/()]+", title or "") if w]
    kept = [w for w in words if _search_tokens(w) and not (_search_tokens(w) <= _NOISE)]
    return " ".join(kept[:6])


# ---------- звідки дані ----------

def matching_sources(title):
    """(товари бота, кандидати «💡» й раніше оцінені товари — [(назва в спостереженнях, запит)]),
    у запиті яких — той самий товар, що в назві."""
    from analysis.discovery import CANDIDATES   # тут: discovery тягне мережеві модулі
    watches = [w for w in list_watches() + list_archived_watches()
               if _title_matches_search(title, w.get("query") or "", "")]
    candidates = [(name, query) for _, name, query, _ in CANDIDATES if _title_matches_search(title, query, "")]
    candidates += [(APPRAISED_PREFIX + p["query"], p["query"]) for p in list_appraised_products()
                   if _title_matches_search(title, p["query"], "")]
    return watches, candidates


def remember_appraised(title, query, watches, candidates, sale=None, total=None):
    """Товару немає ні серед товарів бота, які скануються, ні в «💡» — запам'ятати, щоб бот раз на добу
    збирав по ньому продажі. True — бот збиратиме продажі (новий або вже збирає)."""
    if not query or any(not w.get("archived_at") for w in watches):
        return False
    if any(not name.startswith(APPRAISED_PREFIX) for name, _ in candidates):
        return False
    base = sale or total
    add_appraised_product(query, title, round(base * APPRAISE_FLOOR_SHARE) if base else 0)
    return True


def _groups(spec, laptop):
    """Групи від точної до ширшої. Для ноутбука — не ширше за «чип · екран» / «відеокарта · процесор»."""
    if not spec or spec == "unspecified" or spec.startswith(UNKNOWN_GPU):
        return [] if laptop else ["*"]
    # Відома конфігурація — без «усіх конфігурацій»: iPhone 128GB і 256GB коштують по-різному
    return [g for g in price_fallbacks(spec) if g != "*"]


def _private(rows):
    return [r for r in rows if r.get("cond_group") == RESALE_GROUP and r.get("price")]


def _in_group(rows, group):
    return [r for r in rows if group == "*" or spec_matches(r.get("spec_group") or "unspecified", group)]


def gather(title, spec, laptop, sources=None):
    """Продажі й поточні оголошення 👤 приватних з усіх джерел (без повторів): (sold, active, older).
    sold — за SOLD_LOOKBACK_DAYS днів; older — старіші, до APPRAISE_OLD_DAYS (з бази й «🗃 Архіву продажів»)."""
    watches, candidates = sources if sources is not None else matching_sources(title)
    now = time.time()
    recent_since = now - SOLD_LOOKBACK_DAYS * 86400
    sold, active, older, seen_sold, seen_active = [], [], [], set(), set()

    def add_sold(rows):
        for r in _private(rows):
            if r["item_id"] not in seen_sold and (r.get("gone_at") or 0) >= now - APPRAISE_OLD_DAYS * 86400:
                seen_sold.add(r["item_id"])
                (sold if (r.get("gone_at") or 0) >= recent_since else older).append(r)

    for w in watches:
        add_sold(get_sold_listings(w["id"], LISTING_OBS_RETENTION_DAYS))
        for r in _private(get_current_listings(w["id"])):
            if r["item_id"] not in seen_active:
                seen_active.add(r["item_id"])
                active.append(r)
    for name, _ in candidates:
        add_sold(get_discovery_sold(name, LISTING_OBS_RETENTION_DAYS))
    # «🗃 Архів продажів»: запити тих самих товарів, а також видалених товарів і прибраних кандидатів
    queries = {w["query"] for w in watches} | {q for _, q in candidates}
    queries |= {q for q in archived_queries() if _title_matches_search(title, q, "")}
    add_sold(get_archived_sales(queries, since=now - APPRAISE_OLD_DAYS * 86400))
    return sold, active, older


def live_listings(query, laptop):
    """Разовий пошук на eBay: поточні оголошення 👤 приватних з класом кожного. ≈1 запит."""
    from ebay.ebay_api import search_active_items   # тут: мережевий модуль лише за потреби
    items = search_active_items(query=query, condition_ids=DEFAULT_CONDITION_IDS, exclude_terms=EXCLUDE_TERMS,
                                limit=APPRAISE_SEARCH_LIMIT, fresh=True)
    cached = get_cached_specs([it["item_id"] for it in items]) if laptop else {}
    rows = []
    for it in items:
        if it.get("cond_group") != RESALE_GROUP or not _title_matches_search(it["title"], query, ""):
            continue
        aspects = (cached.get(it["item_id"]) or (None, None))[1] or {}
        spec, _ = classify(it["title"], aspects, it.get("category_names") or (), query)
        rows.append({"item_id": it["item_id"], "price": it["total_price"], "spec_group": spec,
                     "cond_group": it["cond_group"]})
    return rows


# ---------- оцінка ----------

def estimate(title, spec, laptop, query="", live=True, sources=None):
    """Оцінка ціни продажу: dict з полями
       sale, source ('sold' / 'sold_old' / 'listings' / None), group, sold_count, old_count (з них старіших
       за SOLD_LOOKBACK_DAYS), sold_range, sold_days, listings_count, few_sold (є продажі, але замало),
       searched (робився разовий пошук).
    Мережа — лише якщо live і в базі замало оголошень. Викликати з потоку."""
    sold, active, older = gather(title, spec, laptop, sources)
    groups = _groups(spec, laptop)
    result = {"spec": spec, "laptop": laptop, "sale": None, "source": None, "group": None, "sold_count": 0,
              "old_count": 0, "sold_range": None, "sold_days": None, "listings_count": 0, "few_sold": None,
              "searched": False}
    # 1. Продажі: найвужча група, де їх достатньо
    for group in groups:
        rows = _in_group(sold, group)
        if len(rows) >= MIN_SOLD_SAMPLE:
            summary = summarize(rows)
            result.update(sale=recent_median(rows), source="sold", group=group, sold_count=len(rows),
                          sold_range=typical_range([r["price"] for r in rows]),
                          sold_days=summary["median_days"] if summary else None)
            return result
        if rows and result["few_sold"] is None:
            result["few_sold"] = (len(rows), statistics.median(r["price"] for r in rows), group)
    # 2. Свіжих замало — разом зі старішими (2–6 місяців); recent_median дає свіжим більшу вагу
    if older:
        for group in groups:
            recent, old = _in_group(sold, group), _in_group(older, group)
            rows = recent + old
            if old and len(rows) >= MIN_SOLD_SAMPLE:
                summary = summarize(rows)
                result.update(sale=recent_median(rows), source="sold_old", group=group, sold_count=len(rows),
                              old_count=len(old), sold_range=typical_range([r["price"] for r in rows]),
                              sold_days=summary["median_days"] if summary else None)
                return result
    # 3. Оголошення: з бази, а якщо мало — разовий пошук
    pools = [active]
    if live and query:
        try:
            pools.append(live_listings(query, laptop))
            result["searched"] = True
        except Exception as e:   # eBay не відповів — оцінка лише з того, що вже є
            log.debug("Разовий пошук для оцінки не вдався: %s", e)
    seen, listings = set(), []
    for pool in pools:
        for r in pool:
            if r["item_id"] not in seen:
                seen.add(r["item_id"])
                listings.append(r)
    for group in groups:
        prices = filter_outliers([r["price"] for r in _in_group(listings, group)])
        if len(prices) >= APPRAISE_LISTINGS_MIN:
            result.update(sale=percentile(prices, 25), source="listings", group=group, listings_count=len(prices))
            return result
    return result


def profit_for(chat_id, query, title, sale, total):
    """(купувати до, прибуток, мін. прибуток €) для ціни оголошення total — поріг як у товарів тієї ж
    категорії (свій поріг категорії або загальний)."""
    group = auto_watch_group({"query": query or title, "label": title})
    rule = get_category_profits(chat_id).get(group) or ("eur", get_min_profit(chat_id))
    needed = profit_needed(rule, sale)
    return max_buy_price(sale, needed), estimate_resale_profit(sale, total)[1], needed


def appraise(chat_id, text):
    """Повна оцінка за повідомленням користувача (посилання / номер / назва). Мережа — з потоку.
    Повертає dict: title, url, total, cond_group, query, spec, laptop, estimate, buy_limit, profit, min_profit."""
    listing = read_listing(text)
    if listing:
        title = listing["title"]
        watches, candidates = matching_sources(title)
        query = (watches[0]["query"] if watches else candidates[0][1] if candidates else search_query(title))
        spec, laptop = classify(title, listing["aspects"], listing["categories"], query)
    else:
        title = " ".join((text or "").split())
        if len(_search_tokens(title)) < 2:
            raise AppraiseError("Напиши назву детальніше (напр. «MacBook Pro 14 M3 Pro 18GB») або надішли посилання.")
        watches, candidates = matching_sources(title)
        # Запит — як у товару бота з таким самим товаром (спільний ринок), інакше — сама назва
        query = watches[0]["query"] if watches else candidates[0][1] if candidates else title
        spec, laptop = classify(title, query=query)
    est = estimate(title, spec, laptop, query=query, sources=(watches, candidates))
    try:
        collecting = remember_appraised(title, query, watches, candidates, est.get("sale"),
                                        listing["total"] if listing else None)
    except Exception as e:   # не завадить самій оцінці
        log.debug("Не вдалося запам'ятати оцінений товар: %s", e)
        collecting = False
    out = {"title": title, "url": listing["url"] if listing else None, "total": listing["total"] if listing else None,
           "cond_group": listing["cond_group"] if listing else None, "query": query, "spec": spec, "laptop": laptop,
           "estimate": est, "buy_limit": None, "profit": None, "min_profit": None, "collecting": collecting}
    if est["sale"] and out["total"]:
        out["buy_limit"], out["profit"], out["min_profit"] = profit_for(chat_id, query, title, est["sale"], out["total"])
    return out


def group_label(group):
    """«усі конфігурації» / «M3 Pro · 14"» — для тексту оцінки."""
    if not group or group == "*":
        return "усі конфігурації"
    return scoped_label(group) if SEP in group or spec_kind(group) else group
