"""
«🛠 Нерозпізнані»: оголошення, клас яких бот не визначив повністю навіть після того,
як прочитав назву, характеристики й опис. Користувач вирішує, що з ними робити:
вказати клас вручну, залишити як є, позначити «інший товар» чи відкласти приклад
для розробника. Усе — з бази, без запитів до eBay.
"""

from collections import Counter

from db import (
    clear_flagged,
    forget_seen_item,
    get_cached_specs,
    get_current_listings,
    get_flagged,
    get_manual_specs,
    get_market_stats,
    get_reviewed_ids,
    get_watch_categories,
    mark_market_stale,
    save_manual_spec,
    set_listing_spec,
    set_review,
)
from laptops import (
    DESC_CHECKED,
    SEP,
    UNKNOWN_GPU,
    class_sources,
    group_scope,
    is_laptop,
    sources_line,
    wants_aspects,
)
from market import _stat_for_item, dedupe_offers

MAX_UNRECOGNIZED = 50
MAX_OPTIONS = 10


def _is_laptop_watch(watch):
    return is_laptop(query=watch.get("query") or "", category_names=[c["name"] for c in get_watch_categories(watch)])


def _incomplete(spec, laptop):
    spec = spec or "unspecified"
    return wants_aspects(spec) if laptop else spec == "unspecified"


def unrecognized_items(watch):
    """Нерозпізнані оголошення товару — від найцікавішого (найдешевшого відносно ціни групи), до 50.
    Кожне: item_id, title, price, url, cond_group, spec_group, copies, info (що знайдено), ref (ціна групи)."""
    laptop = _is_laptop_watch(watch)
    reviewed = get_reviewed_ids(watch["id"])
    rows = [r for r in get_current_listings(watch["id"])
            if r["item_id"] not in reviewed and _incomplete(r["spec_group"], laptop)]
    if not rows:
        return []
    ids = [r["item_id"] for r in rows]
    manual = get_manual_specs(ids)
    cached = get_cached_specs(ids)
    result = []
    for r in rows:
        if r["item_id"] in manual:
            continue
        entry = cached.get(r["item_id"])
        aspects = entry[1] if entry else None
        # Бот ще не дочитав (характеристики чи опис) — він і так скоро розбере сам
        if laptop and not (aspects and DESC_CHECKED in aspects):
            continue
        if not laptop and entry is None:
            continue
        info = (sources_line(class_sources(r["title"] or "", aspects, watch.get("query") or "")) if laptop
                else "пам'ять / конфігурація: ❓ (ні в назві, ні в характеристиках)")
        result.append(dict(r, info=info))
    result = dedupe_offers(result, price_field="price")

    stats = {(s["cond_group"], s["spec_group"]): s for s in get_market_stats(watch["id"])}
    for it in result:
        stat = _stat_for_item(stats, it)
        sale = (stat["sale_price"] or stat["median_price"]) if stat else None
        it["ref"] = (stat, sale) if sale else None
        it["ratio"] = it["price"] / sale if sale else 9.9
    result.sort(key=lambda it: it["ratio"])
    return result[:MAX_UNRECOGNIZED]


def count_unrecognized(watch):
    return len(unrecognized_items(watch))


def class_options(watch, item):
    """Варіанти класу для «✏️ Вказати вручну»: повні класи, що вже є в цьому товарі,
    узгоджені з тим, що про оголошення відомо (напр. RAM 16GB), — від найпоширеніших."""
    laptop = _is_laptop_watch(watch)
    counts = Counter(r["spec_group"] for r in get_current_listings(watch["id"])
                     if r["spec_group"] and not _incomplete(r["spec_group"], laptop)
                     and not (laptop and group_scope(r["spec_group"])))
    for s in get_market_stats(watch["id"]):
        if s["spec_group"] != "*" and not _incomplete(s["spec_group"], laptop) and not group_scope(s["spec_group"]):
            counts.setdefault(s["spec_group"], 0)
    known = [p for p in (item.get("spec_group") or "").split(SEP)
             if p and p != "unspecified" and not p.startswith(UNKNOWN_GPU)]
    fitting = [spec for spec, _ in counts.most_common() if all(p in spec.split(SEP) for p in known)]
    return (fitting or [spec for spec, _ in counts.most_common()])[:MAX_OPTIONS]


def apply_manual_class(watch, item_id, spec):
    """Клас, вказаний вручну: в історію і в майбутні сканування; ринок перерахується, а оголошення
    оціниться заново (може виявитись вигідним)."""
    save_manual_spec(item_id, spec)
    set_listing_spec(watch["id"], item_id, spec)
    forget_seen_item(watch["id"], item_id)
    mark_market_stale(watch["id"])


def keep_as_is(watch, item):
    set_review(watch["id"], item["item_id"], "keep")


def flag_for_developer(watch, item):
    set_review(watch["id"], item["item_id"], "flag", item.get("title"), item.get("price"), item.get("url"),
               item.get("info"))


def developer_report(watch):
    """Текст для розробника: приклади, де бот не розпізнав клас."""
    rows = get_flagged(watch["id"])
    lines = [f"Нерозпізнані оголошення — «{watch['label']}» (запит: {watch.get('query') or ''})"]
    for n, r in enumerate(rows, 1):
        lines.append(f"\n{n}. {r['title'] or ''}\n   {r['price'] or 0:.0f} €\n   {r['note'] or ''}\n   {r['url'] or ''}")
    return "\n".join(lines), len(rows)


def clear_report(watch):
    clear_flagged(watch["id"])
