"""
Спільний ринок: однакові товари різних користувачів сканують eBay ОДИН раз.

«Той самий товар» — та сама назва (без різниці регістр і порядок слів) і ті самі
категорії. Сканування робиться з найм'якшими налаштуваннями групи (усі стани,
найнижча мінімальна ціна, лише спільні виключені слова), а результат ділиться:
кожен користувач потім відсіює його своїми фільтрами (стан, мін. ціна, виключені
слова, сховані оголошення, характеристики). Особисте — сховані, «інший товар»,
вивчені слова, пропозиції, покупки — лишається в кожного своє.

Новий користувач одразу отримує історію спостережень (зокрема продажі) від
товару-«сусіда», тож ціни й 📈 Продажі в нього з'являються без днів очікування.
"""

import copy
import threading
import time

from settings import log
from textparse import _search_tokens, condition_group_from_item
from db import copy_listing_history, get_watch_categories, list_watches, watch_category_ids
from ebay_api import effective_min_price, search_active_items, search_in_categories

MARKET_CACHE_SECONDS = 45 * 60   # ринкове сканування (раз на годину) — ділиться в межах 45 хв
MANUAL_CACHE_SECONDS = 5 * 60    # ручне «Оновити ціни» — не старіше 5 хв
DEAL_CACHE_SECONDS = 4 * 60      # пошук нових лотів (кожні 5 хв) — ділиться в межах циклу

_cache: dict = {}
_cache_lock = threading.Lock()
_key_locks: dict = {}


def market_key(w):
    return " ".join(sorted(_search_tokens(w["query"]))), tuple(sorted(watch_category_ids(w)))


def query_key(query):
    return " ".join(sorted(_search_tokens(query)))


def siblings(w):
    """Активні товари (усіх користувачів) з тією самою назвою і категоріями, включно з w."""
    key = market_key(w)
    group = [s for s in list_watches(active_only=True) if market_key(s) == key]
    if w.get("id") not in {s["id"] for s in group}:
        group.append(w)
    return group


def _conditions(w):
    return {c.strip() for c in (w.get("condition_ids") or "").split(",") if c.strip()}


def shared_search_kwargs(w):
    """Параметри одного спільного запиту, який покриває потреби всієї групи."""
    group = siblings(w)
    conds = [_conditions(s) for s in group]
    condition_ids = "" if any(not c for c in conds) else ",".join(sorted(set().union(*conds)))
    excludes = [_search_tokens(s.get("exclude") or "") for s in group]
    common_exclude = set.intersection(*excludes) if excludes else set()
    mins = [effective_min_price(s) for s in group]
    min_price = None if any(not m for m in mins) else min(mins)
    # Однаковий текст запиту для всієї групи (найстаріший товар) — щоб і кеш, і eBay бачили один запит
    query = min(group, key=lambda s: s.get("id") or 0)["query"]
    return {"query": query, "condition_ids": condition_ids,
            "exclude_terms": " ".join(sorted(common_exclude)), "min_price": min_price}


def _cached(key, ttl, fetch, shared=True):
    """Результат із кешу, якщо свіжий; інакше — один запит (інші потоки з тим самим
    ключем чекають і беруть готове). Повертає копію — її можна змінювати.
    shared=False (товар без «сусідів») — кеш не потрібен, завжди свіжий запит."""
    if not shared:
        return fetch(), False
    with _cache_lock:
        lock = _key_locks.setdefault(key, threading.Lock())
    with lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return copy.deepcopy(hit[1]), True
        value = fetch()
        with _cache_lock:
            _cache[key] = (time.time(), value)
            # прибираємо старе, щоб кеш не ріс безмежно
            for k in [k for k, (ts, _) in _cache.items() if time.time() - ts > MARKET_CACHE_SECONDS * 2]:
                _cache.pop(k, None)
                _key_locks.pop(k, None)
        return copy.deepcopy(value), False


def own_filter(w, items):
    """Особисті фільтри товару поверх спільної видачі."""
    conds = _conditions(w)
    exclude = _search_tokens(w.get("exclude") or "")
    min_price = effective_min_price(w)
    kept = []
    for it in items:
        if conds and it.get("condition_id") and it["condition_id"] not in conds:
            continue
        if exclude and _search_tokens(it.get("title") or "") & exclude:
            continue
        if min_price and it.get("total_price") is not None and it["total_price"] < min_price:
            continue
        kept.append(it)
    return kept


def market_page(w, category_id, offset, max_age=MARKET_CACHE_SECONDS):
    """Одна сторінка (200) ринкового сканування — спільна для групи. Без особистих фільтрів."""
    kwargs = shared_search_kwargs(w)
    key = ("market", market_key(w), category_id, offset, tuple(sorted(kwargs.items())))
    items, from_cache = _cached(key, max_age, lambda: search_active_items(
        limit=200, offset=offset, fresh=True, category_id=category_id, **kwargs),
        shared=len(siblings(w)) > 1)
    if from_cache:
        log.debug("watch #%s: сторінка ринку з кешу (спільний товар)", w.get("id"))
    return items


def deal_scan(w, limit):
    """Найновіші лоти (пошук вигідних) — спільні для групи, з особистими фільтрами."""
    kwargs = shared_search_kwargs(w)
    key = ("deal", market_key(w), limit, tuple(sorted(kwargs.items())))
    items, _ = _cached(key, DEAL_CACHE_SECONDS, lambda: search_in_categories(
        watch_category_ids(w), limit=limit, fresh=True, **kwargs), shared=len(siblings(w)) > 1)
    return own_filter(w, items)


def find_shared_watch(query, exclude_chat_id=None):
    """Товар іншого користувача з тією самою назвою — щоб запропонувати підключитися."""
    key = query_key(query)
    for w in list_watches(active_only=True):
        if w["chat_id"] != exclude_chat_id and query_key(w["query"]) == key:
            return w
    return None


def attach_shared_history(watch):
    """Новому товару — історія спостережень від «сусідів» (продажі, поточні оголошення)."""
    others = [s for s in siblings(watch) if s["id"] != watch["id"]]
    if not others:
        return 0
    conds = _conditions(watch)
    groups = {condition_group_from_item({"conditionId": c}) for c in conds} if conds else None
    copied = copy_listing_history(
        [s["id"] for s in others], watch["id"],
        allowed_groups=groups,
        exclude_tokens=_search_tokens(watch.get("exclude") or ""),
        min_price=watch.get("min_price") or 0,
    )
    if copied:
        log.info("watch #%s: підключено спільну історію ринку — %s оголошень", watch["id"], copied)
    return copied


def shared_categories(watch):
    return [{"id": c["id"], "name": c["name"]} for c in get_watch_categories(watch)]
