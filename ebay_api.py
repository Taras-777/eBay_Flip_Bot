"""
Клієнт eBay API: авторизація, пошук (Browse), характеристики лотів,
категорії (Taxonomy), облік лімітів запитів.
"""

import base64
import config
import requests
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from settings import (
    BEST_OFFER_ASSUMED_DISCOUNT_PCT,
    DAILY_BROWSE_BUDGET,
    DEFAULT_CONDITION_IDS,
    DELIVERY_COUNTRY,
    EBAY_BUYER_POSTAL_CODE,
    EBAY_MARKETPLACE_ID,
    ITEM_LOCATION_REGION,
    LOCAL_TZ,
    MAX_ASPECT_OPTIONS,
    MAX_CATEGORY_OPTIONS,
    MIN_SELLER_FEEDBACK_PCT,
    MIN_SELLER_FEEDBACK_SCORE,
    log,
)
from textparse import (
    COMPAT_ASPECTS,
    CURRENCY_TO_EUR,
    GENERIC_ASPECTS,
    _search_tokens,
    _title_matches_search,
    condition_group_from_item,
)
from db import get_api_calls_today, get_cached_category_aspects, record_api_call, save_category_aspects


TAXONOMY_BASE = "https://api.ebay.com/commerce/taxonomy/v1"


# ============================================================
# EBAY КЛІЄНТ (Browse API)
# ============================================================

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"


SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"


ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/"


NETWORK_MAX_ATTEMPTS = 4


NETWORK_BACKOFF_SECONDS = 1.0


NETWORK_MAX_BACKOFF_SECONDS = 30.0


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


_http_local = threading.local()


def _http_session():
    """Окрема requests.Session на кожен потік: з'єднання з eBay лишається
    відкритим між запитами (без нового TLS-рукостискання щоразу), а потоки
    не ділять одну сесію між собою."""
    session = getattr(_http_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8)
        session.mount("https://", adapter)
        _http_local.session = session
    return session


_token_cache = {"token": None, "expires_at": 0}


_token_lock = threading.Lock()


def _retry_after_seconds(response):
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, min(float(value), NETWORK_MAX_BACKOFF_SECONDS))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(
                0.0,
                min(
                    (retry_at - datetime.now(timezone.utc)).total_seconds(),
                    NETWORK_MAX_BACKOFF_SECONDS,
                ),
            )
        except (TypeError, ValueError, OverflowError):
            return None


def _request_with_retries(method, url, **kwargs):
    """Виконує HTTP-запит з обмеженим retry для тимчасових помилок."""
    for attempt in range(1, NETWORK_MAX_ATTEMPTS + 1):
        try:
            response = _http_session().request(method, url, **kwargs)
            if url == SEARCH_URL:
                record_api_call("browse")
            elif url.startswith(TAXONOMY_BASE):
                record_api_call("taxonomy")   # окрема квота Taxonomy API
            elif url.startswith(ITEM_URL):
                record_api_call("browse")   # getItem — теж Browse API, та сама квота
                record_api_call("item")
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt == NETWORK_MAX_ATTEMPTS:
                log.error(
                    "Мережевий запит %s %s завершився після %s спроб: %s",
                    method, url, attempt, exc,
                )
                raise
            delay = min(
                NETWORK_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                NETWORK_MAX_BACKOFF_SECONDS,
            )
            log.warning(
                "Тимчасова мережева помилка %s %s (спроба %s/%s): %s; повтор через %.1fs",
                method, url, attempt, NETWORK_MAX_ATTEMPTS, exc, delay,
            )
            time.sleep(delay)
            continue

        if response.status_code not in RETRYABLE_STATUS_CODES:
            return response

        if attempt == NETWORK_MAX_ATTEMPTS:
            log.error(
                "HTTP %s для %s %s після %s спроб",
                response.status_code, method, url, attempt,
            )
            return response

        delay = _retry_after_seconds(response)
        if delay is None:
            delay = min(
                NETWORK_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                NETWORK_MAX_BACKOFF_SECONDS,
            )
        log.warning(
            "Тимчасова HTTP-помилка %s для %s %s (спроба %s/%s); повтор через %.1fs",
            response.status_code, method, url, attempt, NETWORK_MAX_ATTEMPTS, delay,
        )
        time.sleep(delay)

    raise RuntimeError("HTTP retry loop завершився без відповіді")


def _get_access_token():
    """
    _get_access_token() (і search_active_items(), який його викликає)
    запускається через asyncio.to_thread — тобто в окремому ОС-потоці.
    Фонова перевірка (scheduler) і, наприклад, команда /addwatch від
    користувача можуть викликати це ОДНОЧАСНО з різних потоків. Без
    блокування обидва могли б одночасно побачити прострочений кеш і
    зробити два зайві запити OAuth-токена одночасно — блокування
    прибирає цю гонку.
    """
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    with _token_lock:
        # Поки чекали на lock, інший потік міг уже оновити токен
        now = time.time()
        if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
            return _token_cache["token"]

        creds = f"{config.EBAY_CLIENT_ID}:{config.EBAY_CLIENT_SECRET}"
        b64_creds = base64.b64encode(creds.encode()).decode()

        resp = _request_with_retries(
            "POST",
            OAUTH_URL,
            headers={
                "Authorization": f"Basic {b64_creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["token"] = data["access_token"]
        # Час дії рахуємо від моменту ОТРИМАННЯ відповіді, а не від початку
        # запиту — інакше кешований токен вважатиметься дійсним трохи довше,
        # ніж насправді дозволяє eBay (на час мережевої затримки запиту).
        _token_cache["expires_at"] = time.time() + data["expires_in"]
        return _token_cache["token"]


ANALYTICS_RATE_LIMIT_URL = "https://api.ebay.com/developer/analytics/v1_beta/rate_limit/"


_rate_limit_cache = {"data": None, "fetched_at": 0}


def fetch_browse_rate_limit():
    """
    Офіційні дані eBay про використання Browse API (getRateLimits):
    ліміт, зроблено, залишилось, час скидання. Враховує ВСІ запити з цих
    ключів, не лише з цього сервера. Результат кешується для меню.
    """
    token = _get_access_token()
    resp = _request_with_retries(
        "GET", ANALYTICS_RATE_LIMIT_URL,
        headers={"Authorization": "Bearer " + token},
        params={"api_context": "buy", "api_name": "browse"},
        timeout=15,
    )
    resp.raise_for_status()
    daily = None
    for api in resp.json().get("rateLimits") or []:
        for resource in api.get("resources") or []:
            for rate in resource.get("rates") or []:
                window = int(rate.get("timeWindow") or 0)
                # Цікавить добовий ліміт (86400 с); коротші вікна — лише обмеження частоти
                if daily is None or abs(window - 86400) < abs(int(daily.get("timeWindow") or 0) - 86400):
                    daily = rate
    if daily is None:
        return None
    limit = int(daily.get("limit") or 0)
    remaining = int(daily.get("remaining") or 0)
    data = {
        "limit": limit,
        "remaining": remaining,
        "count": int(daily.get("count") if daily.get("count") is not None else max(limit - remaining, 0)),
        "reset": _parse_ebay_ts(daily.get("reset")),
    }
    _rate_limit_cache.update(data=data, fetched_at=time.time())
    return data


def fetch_item_aspects(item_id):
    """Характеристики лота (localizedAspects) через Browse getItem →
    {назва в нижньому регістрі: значення}. Порожній dict, якщо лот уже зник."""
    token = _get_access_token()
    resp = _request_with_retries(
        "GET", ITEM_URL + quote(item_id, safe=""),
        headers={
            "Authorization": "Bearer " + token,
            "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country={DELIVERY_COUNTRY},zip={EBAY_BUYER_POSTAL_CODE}",
        },
        timeout=15,
    )
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    return {
        (a.get("name") or "").strip().lower(): a.get("value") or ""
        for a in resp.json().get("localizedAspects") or []
    }


_category_tree = {"id": None}


def get_category_tree_id():
    """ID дерева категорій маркетплейсу (EBAY_DE) — один раз за запуск."""
    if _category_tree["id"]:
        return _category_tree["id"]
    token = _get_access_token()
    resp = _request_with_retries(
        "GET", f"{TAXONOMY_BASE}/get_default_category_tree_id",
        headers={"Authorization": "Bearer " + token},
        params={"marketplace_id": EBAY_MARKETPLACE_ID},
        timeout=15,
    )
    resp.raise_for_status()
    _category_tree["id"] = resp.json()["categoryTreeId"]
    return _category_tree["id"]


def fetch_category_aspects(category_id):
    """
    Характеристики категорії (getItemAspectsForCategory): список
    {name, required, usage}. required=True — продавець не може виставити
    лот без цієї характеристики. Кешується на CATEGORY_ASPECTS_CACHE_DAYS днів.
    """
    cached = get_cached_category_aspects(category_id)
    if cached is not None:
        return cached
    token = _get_access_token()
    resp = _request_with_retries(
        "GET", f"{TAXONOMY_BASE}/category_tree/{get_category_tree_id()}/get_item_aspects_for_category",
        headers={"Authorization": "Bearer " + token},
        params={"category_id": str(category_id)},
        timeout=20,
    )
    resp.raise_for_status()
    aspects = []
    for a in resp.json().get("aspects") or []:
        constraint = a.get("aspectConstraint") or {}
        name = a.get("localizedAspectName")
        if name:
            aspects.append({
                "name": name,
                "required": bool(constraint.get("aspectRequired")),
                "usage": constraint.get("aspectUsage") or "",
            })
    save_category_aspects(category_id, aspects)
    return aspects


def find_leaf_category(query, category_id):
    """
    getItemAspectsForCategory працює лише з КІНЦЕВИМИ категоріями. Якщо обрана
    категорія батьківська (напр. "Computer, Tablets & Netzwerk"), шукаємо в ній
    товар і беремо кінцеву підкатегорію (leafCategoryIds), де найбільше
    оголошень, що проходять перевірку назви, — тобто самого товару, а не аксесуарів.
    """
    data = _browse_search(query, condition_ids=DEFAULT_CONDITION_IDS, limit=50, category_id=category_id)
    counts = {}
    for it in data.get("itemSummaries") or []:
        if not _title_matches_search(it.get("title") or "", query, ""):
            continue
        for leaf in it.get("leafCategoryIds") or []:
            counts[str(leaf)] = counts.get(str(leaf), 0) + 1
    return max(counts, key=counts.get) if counts else None


def aspect_options_for_category(category_id, query=None):
    """Характеристики, які варто пропонувати як обов'язкові: без загальних
    (марка, колір…) і без "сумісних" (ті означають аксесуар). Обов'язкові
    категорії — першими, далі рекомендовані eBay."""
    try:
        aspects = fetch_category_aspects(category_id)
    except requests.exceptions.HTTPError as e:
        not_leaf = e.response is not None and e.response.status_code == 400
        if not (not_leaf and query):
            raise
        leaf = find_leaf_category(query, category_id)
        if not leaf:
            raise
        log.info("Категорія %s не кінцева — беру характеристики з підкатегорії %s", category_id, leaf)
        aspects = fetch_category_aspects(leaf)
        save_category_aspects(category_id, aspects)  # наступного разу — одразу з кешу
    candidates = [
        a for a in aspects
        if a["name"].lower() not in GENERIC_ASPECTS and a["name"].lower() not in COMPAT_ASPECTS
    ]
    candidates.sort(key=lambda a: (not a["required"], a["usage"] != "RECOMMENDED"))
    return candidates[:MAX_ASPECT_OPTIONS]


def aspect_options_for_categories(category_ids, query=None):
    """Характеристики кількох категорій разом (без повторів); обов'язкова,
    якщо обов'язкова хоча б в одній із категорій."""
    merged = {}
    for cid in category_ids:
        for opt in aspect_options_for_category(cid, query):
            prev = merged.get(opt["name"])
            if prev is None:
                merged[opt["name"]] = dict(opt)
            elif opt["required"]:
                prev["required"] = True
    options = list(merged.values())
    options.sort(key=lambda a: (not a["required"], a["usage"] != "RECOMMENDED"))
    return options[:MAX_ASPECT_OPTIONS + 2]


def browse_budget_left():
    """
    Скільки ще запитів Browse API бот може зробити сьогодні в межах
    DAILY_BROWSE_BUDGET. Найточніше — з офіційних даних eBay (враховують і
    інші копії бота з тими ж ключами); якщо вони застарі — з власного лічильника.
    """
    data = _rate_limit_cache["data"]
    if data and time.time() - _rate_limit_cache["fetched_at"] < 30 * 60:
        return data["remaining"] - max(data["limit"] - DAILY_BROWSE_BUDGET, 0)
    return DAILY_BROWSE_BUDGET - get_api_calls_today("browse")


def api_usage_line():
    """Рядок для головного меню власника."""
    data = _rate_limit_cache["data"]
    if data:
        line = (f"📡 Запити до eBay сьогодні: <b>{data['count']}</b> / {data['limit']} "
                f"(залишилось {data['remaining']}; бот тримається до ~{DAILY_BROWSE_BUDGET})")
        if data["reset"]:
            reset_local = datetime.fromtimestamp(data["reset"], LOCAL_TZ).strftime("%H:%M")
            line += f"\n🔄 Ліміт скинеться о {reset_local}"
        updated = datetime.fromtimestamp(_rate_limit_cache["fetched_at"], LOCAL_TZ).strftime("%H:%M")
        return line + f"\n<i>дані eBay, оновлено о {updated}</i>"
    return (f"📡 Запити до eBay сьогодні: <b>{get_api_calls_today()}</b>\n"
            "<i>підрахунок бота; офіційні дані eBay ще не отримані</i>")


def _browse_search(query, condition_ids="", exclude_terms="", limit=50,
                   category_id=None, min_price=None, fieldgroups=None, fresh=False,
                   sort="newlyListed", offset=0):
    """
    Спільний низькорівневий запит до item_summary/search.

    Категорія передається окремим параметром category_ids — у Browse API
    немає поля categoryIds всередині filter, тож такий фільтр eBay просто
    ігнорує. ID категорій різні на кожному маркетплейсі, тому вони
    беруться з відповіді самого ebay.de (get_category_options), а не
    хардкодяться.

    Мінімальна ціна — filter price:[X..] разом з priceCurrency:EUR
    (без валюти eBay фільтр ціни не застосовує).
    """
    token = _get_access_token()

    q = query
    for term in _search_tokens(exclude_terms):
        q += f" -{term}"

    params = {"q": q, "limit": str(min(max(limit, 1), 100)), "sort": sort}
    if offset:
        params["offset"] = str(offset)
    if category_id:
        params["category_ids"] = str(category_id)
    if fieldgroups:
        params["fieldgroups"] = fieldgroups

    filters_list = []
    if condition_ids:
        # eBay Browse API вимагає pipe ('|') як роздільник для
        # багатозначних фільтрів на кшталт conditionIds — у БД значення
        # зберігаються через кому (зручніше для CONDITION_PRESETS),
        # тому конвертуємо тут.
        ids_piped = condition_ids.replace(",", "|")
        filters_list.append(f"conditionIds:{{{ids_piped}}}")
    filters_list.append("buyingOptions:{FIXED_PRICE}")
    filters_list.append(f"itemLocationRegion:{ITEM_LOCATION_REGION}")
    filters_list.append(f"deliveryCountry:{DELIVERY_COUNTRY}")
    if min_price and float(min_price) > 0:
        filters_list.append(f"price:[{float(min_price):.2f}..]")
        filters_list.append("priceCurrency:EUR")
    params["filter"] = ",".join(filters_list)

    headers = {
        "Authorization": "Bearer " + token,
        "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
        "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country={DELIVERY_COUNTRY},zip={EBAY_BUYER_POSTAL_CODE}",
        "Cache-Control": "no-cache, no-store",
        "Pragma": "no-cache",
        "Content-Type": "application/json",
    }
    if fresh:
        headers["X-EBAY-C-REQUEST-ID"] = str(uuid.uuid4())

    resp = _request_with_retries("GET", SEARCH_URL, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_category_options(query, condition_ids=DEFAULT_CONDITION_IDS):
    """
    Питає в eBay, в яких категоріях ebay.de є результати за цим запитом
    (fieldgroups=CATEGORY_REFINEMENTS → refinement.categoryDistributions).
    Повертає список {id, name, count}, найбільші категорії першими.
    """
    data = _browse_search(
        query, condition_ids=condition_ids, limit=1, fieldgroups="CATEGORY_REFINEMENTS",
    )
    refinement = data.get("refinement") or {}
    options = []
    for dist in refinement.get("categoryDistributions") or []:
        cat_id = dist.get("categoryId")
        if not cat_id:
            continue
        options.append({
            "id": str(cat_id),
            "name": dist.get("categoryName") or f"Категорія {cat_id}",
            "count": int(dist.get("matchCount") or 0),
        })
    options.sort(key=lambda o: o["count"], reverse=True)
    return options[:MAX_CATEGORY_OPTIONS]


def search_active_items(query, condition_ids="", exclude_terms="", limit=50, fresh=False,
                        category_id=None, min_price=None, sort="newlyListed", offset=0):
    """
    Пошук активних оголошень через eBay Browse API.
    Сортування — за датою публікації (newlyListed), а не за ціною.
    Аукціони виключаються фільтром buyingOptions:{FIXED_PRICE} і
    додатково клієнтською перевіркою. Лоти з опцією BEST_OFFER
    позначаються окремим прапорцем.

    Регіон: лот мусить бути в межах ЄС (itemLocationRegion:EUROPEAN_UNION),
    і мусить бути можливість доставки саме в Німеччину (deliveryCountry).
    Категорія й мінімальна ціна — див. _browse_search().
    """
    data = _browse_search(
        query, condition_ids=condition_ids, exclude_terms=exclude_terms, limit=limit,
        category_id=category_id, min_price=min_price, fresh=fresh, sort=sort, offset=offset,
    )

    items = []
    for it in data.get("itemSummaries", []):
        title = it.get("title") or ""
        if not _title_matches_search(title, query, exclude_terms):
            continue

        buying_options = it.get("buyingOptions") or []
        if "FIXED_PRICE" not in buying_options:
            continue
        has_best_offer = "BEST_OFFER" in buying_options

        cond_group = condition_group_from_item(it)
        if cond_group == "parts":
            continue  # "на запчастини" — не той товар, що ми перепродаємо

        price_info = it.get("price", {})
        try:
            price = float(price_info.get("value"))
        except (TypeError, ValueError):
            continue

        item_currency = price_info.get("currency", "EUR").upper()
        exchange_rate = CURRENCY_TO_EUR.get(item_currency)
        if exchange_rate is None:
            log.debug("Пропущено лот %s: невідома валюта %s", it.get("itemId"), item_currency)
            continue

        shipping_cost = 0.0
        shipping_options = it.get("shippingOptions") or []
        if shipping_options:
            costs = []
            for opt in shipping_options:
                cost_info = opt.get("shippingCost", {})
                try:
                    costs.append(float(cost_info.get("value", 0)))
                except (TypeError, ValueError):
                    pass
            if costs:
                shipping_cost = min(costs)

        seller = it.get("seller") or {}
        feedback_score = seller.get("feedbackScore")
        feedback_pct = seller.get("feedbackPercentage")
        try:
            feedback_pct = float(feedback_pct) if feedback_pct is not None else None
        except (TypeError, ValueError):
            feedback_pct = None

        suspicious = False
        if feedback_score is not None and feedback_score < MIN_SELLER_FEEDBACK_SCORE:
            suspicious = True
        if feedback_pct is not None and feedback_pct < MIN_SELLER_FEEDBACK_PCT:
            suspicious = True

        total_price = (price + shipping_cost) * exchange_rate
        effective_price = (
            total_price * (1 - BEST_OFFER_ASSUMED_DISCOUNT_PCT / 100)
            if has_best_offer else total_price
        )

        items.append(
            {
                "item_id": it.get("itemId"),
                "title": title,
                "price": price,
                "shipping_cost": shipping_cost,
                "total_price": total_price,
                "effective_price": effective_price,
                "has_best_offer": has_best_offer,
                "currency": "EUR",
                "url": it.get("itemWebUrl"),
                "condition": it.get("condition"),
                "cond_group": cond_group,
                "created_at": _parse_ebay_ts(it.get("itemCreationDate")),
                "end_at": _parse_ebay_ts(it.get("itemEndDate")),
                "seller_feedback_score": feedback_score,
                "seller_feedback_pct": feedback_pct,
                "suspicious": suspicious,
            }
        )
    return items


def _parse_ebay_ts(value):
    """ISO-дата eBay ("2026-09-20T10:00:00.000Z") → unix timestamp або None."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def _watch_search_kwargs(w):
    """Параметри пошуку, збережені для конкретного відстеження (без категорій —
    їх перебирає search_in_categories)."""
    return {
        "query": w["query"],
        "condition_ids": w["condition_ids"],
        "exclude_terms": w["exclude"],
        "min_price": w.get("min_price") or None,
    }


def search_in_categories(category_ids, sort="newlyListed", **kwargs):
    """
    Browse API приймає лише ОДНУ категорію на запит, тож шукаємо в кожній
    окремо й об'єднуємо без дублікатів. Без категорій — один запит без обмеження.
    Кожна додаткова категорія = ще один запит до eBay.
    """
    merged, seen = [], set()
    for cid in (list(category_ids) or [None]):
        for it in search_active_items(category_id=cid, sort=sort, **kwargs):
            if it["item_id"] and it["item_id"] not in seen:
                seen.add(it["item_id"])
                merged.append(it)
    if sort == "price":
        merged.sort(key=lambda it: it["total_price"])
    return merged