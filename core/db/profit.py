"""
Мінімальний прибуток: товар → категорія → загальний; фільтр прибутку в SQL.
"""

import time

from core import settings


# ---------- мінімальний прибуток: товар → категорія → загальний ----------
# Правило — ("eur", 60) або ("pct", 12): у відсотках — від ціни продажу (12% від 868€ = ~104€).

def parse_profit_rule(value):
    """«60» → ("eur", 60.0); «12%» → ("pct", 12.0); порожнє чи зіпсоване → None."""
    text = str(value or "").strip()
    try:
        if text.endswith("%"):
            return ("pct", float(text[:-1]))
        return ("eur", float(text)) if text else None
    except ValueError:
        return None


def profit_rule_label(rule):
    kind, value = rule
    return f"{value:g}%" if kind == "pct" else f"{value:.0f}€"


def profit_needed(rule, sale):
    """Скільки € прибутку потрібно за правилом при ціні продажу sale."""
    kind, value = rule
    return value * float(sale or 0) / 100 if kind == "pct" else value


def get_category_profit(chat_id, group):
    """Правило мін. прибутку категорії («🎮 Консолі», власна папка «c<id>») або None — як загальний."""
    return parse_profit_rule(get_meta(f"cat_profit:{chat_id}:{group}"))


def get_category_profits(chat_id):
    """{ключ категорії: правило} — лише категорії зі своїм значенням."""
    prefix = f"cat_profit:{chat_id}:"
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE ?", (prefix + "%",)).fetchall()
    rules = {r["key"][len(prefix):]: parse_profit_rule(r["value"]) for r in rows}
    return {k: v for k, v in rules.items() if v}


def _category_watches(chat_id, group):
    from analysis.market import watch_group   # тут: analysis.market сам імпортує db
    custom = {f["id"] for f in list_folders(chat_id)}
    return [w for w in list_watches(chat_id=chat_id) if w.get("min_profit") is None
            and watch_group(w, custom) == group]


def set_category_profit(chat_id, group, value):
    """value — «60», «12%» або None (як загальний). Відкинуті раніше оголошення товарів цієї
    категорії (без свого значення) оцінюються заново: поріг міг знизитись."""
    key = f"cat_profit:{chat_id}:{group}"
    with get_conn() as conn:
        if value is None:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
    reset_unnotified_seen([w["id"] for w in _category_watches(chat_id, group)])


_GROUP_CACHE: dict = {}   # (id товару, папка, запит, чат) → (час, категорія): автовизначення читає оголошення
GROUP_CACHE_SECONDS = 600


def _cached_group(watch, custom_ids=None):
    from analysis.market import watch_group   # тут: analysis.market сам імпортує db
    key = (watch.get("id"), watch.get("folder"), watch.get("query"), watch.get("chat_id"))
    hit = _GROUP_CACHE.get(key)
    if hit and time.time() - hit[0] < GROUP_CACHE_SECONDS and key[0] is not None:
        return hit[1]
    group = watch_group(watch, custom_ids)
    _GROUP_CACHE[key] = (time.time(), group)
    return group


def min_profit_rule(watch, category_rules=None, custom_ids=None):
    """(правило, звідки): свій у товару («own») → категорії («category») → загальний («general»).
    category_rules / custom_ids — заздалегідь прочитані (для багатьох товарів одразу)."""
    own = watch.get("min_profit")
    if own is not None:
        return ("eur", float(own)), "own"
    rules = category_rules if category_rules is not None else get_category_profits(watch["chat_id"])
    if rules:
        rule = rules.get(_cached_group(watch, custom_ids))
        if rule:
            return rule, "category"
    return ("eur", get_min_profit(watch["chat_id"])), "general"


def watch_min_profit(watch, sale=None):
    """Мінімальний прибуток товару в € при ціні продажу sale (потрібна для правила у %)."""
    rule, _ = min_profit_rule(watch)
    if rule[0] == "pct" and sale is None:
        return get_min_profit(watch["chat_id"])   # ціни продажу ще немає — загальний поріг
    return profit_needed(rule, sale)


def min_profit_rules(chat_id=None):
    """{id товару: правило} для всіх активних товарів (користувача) — для фільтрів у SQL."""
    result, cache = {}, {}
    for w in list_watches(chat_id=chat_id):
        cid = w["chat_id"]
        if cid not in cache:
            cache[cid] = (get_category_profits(cid), {f["id"] for f in list_folders(cid)})
        result[w["id"]] = min_profit_rule(w, *cache[cid])[0]
    return result


def set_watch_min_profit(watch, value):
    """value=None — як категорія / загальний. Відкинуті раніше оголошення оцінюються заново."""
    with get_conn() as conn:
        conn.execute("UPDATE watches SET min_profit = ? WHERE id = ?", (value, watch["id"]))
    reset_unnotified_seen([watch["id"]])


def reset_unnotified_seen(watch_ids):
    """Оголошення, які бот бачив і не пропонував (невигідні), — оцінити заново наступним скануванням."""
    ids = list(watch_ids)
    if not ids:
        return
    with get_conn() as conn:
        conn.execute(f"DELETE FROM seen_items WHERE last_notified_price IS NULL AND watch_id IN "
                     f"({','.join('?' * len(ids))})", ids)


def _min_profit_sql(conn, chat_id=None):
    """Мін. прибуток у SQL: функція mp(id товару, ціна продажу) за правилами товару / категорії /
    загальним (правило у % залежить від ціни продажу, тож простого COALESCE замало)."""
    rules = min_profit_rules(chat_id)
    default = ("eur", float(settings.MIN_PROFIT_EUR))
    conn.create_function("mp", 2, lambda wid, sale: profit_needed(rules.get(wid, default), sale),
                         deterministic=True)
    return "mp(w.id, d.median_price)"


def get_min_profit(chat_id):
    """Мінімальний прибуток користувача (€), з якого пропозиція вважається вигідною."""
    try:
        return float(get_meta(f"min_profit:{chat_id}") or settings.MIN_PROFIT_EUR)
    except ValueError:
        return float(settings.MIN_PROFIT_EUR)


def set_min_profit(chat_id, value):
    old = get_min_profit(chat_id)
    set_meta(f"min_profit:{chat_id}", value)
    if float(value) < old:   # поріг знизився — уже відкинуті оголошення оцінюються заново
        reset_unnotified_seen([w["id"] for w in list_watches(chat_id=chat_id) if w.get("min_profit") is None])


# Імпорти з інших частин бази — унизу, щоб модулі могли посилатися один на одного
from core.db.base import get_conn, get_meta, set_meta  # noqa: E402
from core.db.watches import list_folders, list_watches  # noqa: E402
