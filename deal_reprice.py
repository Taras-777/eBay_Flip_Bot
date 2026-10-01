"""
«🔥 Вигідні пропозиції»: переоцінка за свіжою статистикою ринку.

«Продати» записується в момент знахідки. Відтоді бот міг зібрати продажі,
розділити ноутбуки на класи або прибрати з історії запчастини, тож стара
оцінка застаріває. Перед показом (і у фоні) кожна пропозиція отримує ціну з
теперішньої статистики своєї групи. Якщо прибуток опустився нижче
мінімального, пропозиція зникає зі списку (статус не змінюється: якщо ринок
підросте або користувач знизить мінімум, вона повернеться).

Запитів до eBay тут немає, лише дані з бази.
"""

from db import (
    get_cached_specs,
    get_listing_groups,
    get_market_stats,
    get_min_profit,
    get_open_deals,
    get_watch_categories,
    list_watches,
    set_deals_status,
    update_deal_price,
)
from laptops import is_laptop, laptop_spec
from laptops import needs_aspects as laptop_needs_aspects
from market import _stat_for_item, estimate_resale_profit
from textparse import _group_label, extract_spec_key, plural


def price_basis(source, sample):
    """Текст «на чому базується ціна» для картки."""
    source = source or ""
    if "продан" in source:
        return source
    if sample:
        return (f"за {plural(sample, 'поточним оголошенням', 'поточними оголошеннями', 'поточними оголошеннями')}"
                " (продажів ще мало)")
    return source or "оцінка"


def basis_line(d):
    """Рядок картки: звідки взялася ціна «продати». Порожньо для старих записів без даних."""
    if not d.get("sale_source") or not d.get("cond_group"):
        return ""
    line = f"📊 Ціна продажу {price_basis(d['sale_source'], d.get('sale_sample'))} · {_group_label(d['cond_group'], d['spec_group'] or '*')}"
    own = d.get("item_spec") or "unspecified"
    if d["spec_group"] == "*" and own not in ("unspecified", "*"):
        line += "\n⚠️ Для цієї конфігурації даних замало — ціна за всіма, перевір сам"
    return line


def _own_spec(watch, laptop_watch, item_id, title):
    """Конфігурація оголошення з назви і збережених характеристик (як при знахідці)."""
    cached = get_cached_specs([item_id]).get(item_id)
    if laptop_watch or is_laptop(title):
        return laptop_spec(title, cached[1] if cached else None, query=watch.get("query") or "")
    spec = extract_spec_key(title)
    if spec == "unspecified" and cached:
        spec = cached[0]
    return spec


def reprice_deals(chat_id=None):
    """Переоцінює пропозиції без рішення. Повертає, скільки з них перестали бути вигідними
    (були в списку, а тепер прибуток нижче мінімуму)."""
    deals = get_open_deals(chat_id)
    if not deals:
        return 0
    watches = {w["id"]: w for w in list_watches(active_only=True)}
    stats_by_watch, groups_by_watch, laptop_by_watch, min_profit_by_chat = {}, {}, {}, {}
    removed, stale = 0, []
    for d in deals:
        wid, watch = d["watch_id"], watches.get(d["watch_id"])
        if watch is None:
            continue
        if wid not in stats_by_watch:
            stats_by_watch[wid] = {(s["cond_group"], s["spec_group"]): s for s in get_market_stats(wid)}
            groups_by_watch[wid] = get_listing_groups(wid, [x["item_id"] for x in deals if x["watch_id"] == wid])
            laptop_by_watch[wid] = is_laptop(query=watch.get("query") or "",
                                             category_names=[c["name"] for c in get_watch_categories(watch)])
        stats = stats_by_watch[wid]
        if not stats:
            continue   # ринок ще не пораховано (напр. одразу після запуску) — нічого не чіпаємо
        min_profit = min_profit_by_chat.setdefault(d["chat_id"], get_min_profit(d["chat_id"]))
        was_shown = estimate_resale_profit(d["median_price"], d["total_price"])[1] >= min_profit

        known = groups_by_watch[wid].get(d["item_id"])
        if known:
            cond, spec = known
        else:
            cond = d["cond_group"]
            spec = d.get("item_spec") or _own_spec(watch, laptop_by_watch[wid], d["item_id"], d["title"])
        if not cond:
            continue
        if (laptop_by_watch[wid] or is_laptop(d["title"])) and laptop_needs_aspects(spec or "unspecified"):
            stale.append(d["id"])   # ноутбук без відомої відеокарти: таких бот більше не пропонує
            removed += was_shown
            continue
        stat = _stat_for_item(stats, {"cond_group": cond, "spec_group": spec})
        if stat is None:
            continue
        sale = stat["sale_price"] or stat["median_price"]
        changed = (abs(sale - d["median_price"]) >= 0.5 or d.get("sale_source") != stat["sale_source"]
                   or d.get("spec_group") != stat["spec_group"] or d.get("item_spec") != spec
                   or d.get("sale_sample") != stat["sample_size"])
        if changed:
            update_deal_price(d["id"], sale, (sale - d["total_price"]) / sale * 100 if sale else 0,
                              stat["cond_group"], stat["spec_group"], spec,
                              stat["sale_source"], stat["sample_size"])
        if was_shown and estimate_resale_profit(sale, d["total_price"])[1] < min_profit:
            removed += 1
    if stale:
        set_deals_status(stale, "stale")
    return removed
