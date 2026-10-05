"""
«🛠 Нерозпізнані»: оголошення, клас яких бот не визначив повністю навіть після того,
як прочитав назву, характеристики й опис. Користувач вирішує, що з ними робити:
вказати клас вручну, залишити як є, позначити «інший товар» чи відкласти приклад
для розробника. Усе — з бази, без запитів до eBay.
"""

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

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
    list_watches,
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
    spec_kind,
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
    all_rows = get_current_listings(watch["id"])
    # Ноутбук — якщо так позначено товар або більшість його оголошень мають клас ноутбука
    laptop = item_kind(watch, {}, [r["spec_group"] for r in all_rows]) is not None
    reviewed = get_reviewed_ids(watch["id"])
    rows = [r for r in all_rows if r["item_id"] not in reviewed and _incomplete(r["spec_group"], laptop)]
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


RECHECK_MINUTES = 30
_checked: dict = {}   # item_id → коли перевіряли, що ще продається (щоб не питати eBay щоразу)


def drop_unavailable(watch, items):
    """Перед показом: чи оголошення ще продаються. Продані чи завершені більше не показуються
    (у базі позначаються «gone» лише для цього списку — статистику продажів веде звичайне сканування).
    Викликати з потоку. Повертає кількість прибраних."""
    from deal_check import listing_available   # тут, щоб не було циклу імпортів
    now = time.time()
    todo = [it for it in items if now - _checked.get(it["item_id"], 0) > RECHECK_MINUTES * 60]
    if not todo:
        return 0

    def check(it):
        available = listing_available(it["item_id"])
        if available is False:
            set_review(watch["id"], it["item_id"], "gone")
            return 1
        if available:
            _checked[it["item_id"]] = now
        return 0

    with ThreadPoolExecutor(max_workers=min(5, len(todo))) as pool:
        return sum(pool.map(check, todo))


def count_unrecognized(watch):
    return len(unrecognized_items(watch))


def unrecognized_by_watch(chat_id):
    """[(товар, кількість)] для «🛠 Нерозпізнані» в головному меню — від найбільшої кількості."""
    result = []
    for w in list_watches(chat_id=chat_id, active_only=True):
        try:
            n = count_unrecognized(w)
        except Exception:   # один товар з помилкою не має ламати меню
            n = 0
        if n:
            result.append((w, n))
    result.sort(key=lambda wn: -wn[1])
    return result


def total_unrecognized(chat_id):
    return sum(n for _, n in unrecognized_by_watch(chat_id))


def item_kind(watch, item, specs=None):
    """Тип класу, який підходить оголошенню: 'mac', 'win' або None (не ноутбук). Якщо сам товар
    не позначено як ноутбук, вирішує більшість оголошень товару (eBay відносить їх до ноутбуків)."""
    own = spec_kind(item.get("spec_group"))
    if own:
        return own
    if specs is None:
        specs = [r["spec_group"] for r in get_current_listings(watch["id"])]
    kinds = Counter(k for k in (spec_kind(s) for s in specs) if k)
    laptop = _is_laptop_watch(watch) or sum(kinds.values()) * 2 >= len(specs) > 0
    if not laptop:
        return None
    return "mac" if kinds["mac"] > kinds["win"] else "win"


def class_options(watch, item):
    """Варіанти класу для «✏️ Вказати вручну»: повні класи, що вже є в цьому товарі, того ж типу
    (для ноутбука — лише ноутбукові), узгоджені з уже відомим (напр. RAM 16GB), — від найпоширеніших."""
    rows = get_current_listings(watch["id"])
    kind = item_kind(watch, item, [r["spec_group"] for r in rows])
    laptop = kind is not None

    def usable(spec):
        if not spec or _incomplete(spec, laptop) or spec_kind(spec) != kind:
            return False
        return not (laptop and group_scope(spec))

    counts = Counter(r["spec_group"] for r in rows if usable(r["spec_group"]))
    for st in get_market_stats(watch["id"]):
        if st["spec_group"] != "*" and usable(st["spec_group"]):
            counts.setdefault(st["spec_group"], 0)
    known = [p for p in (item.get("spec_group") or "").split(SEP)
             if p and p != "unspecified" and not p.startswith(UNKNOWN_GPU)]
    fitting = [spec for spec, _ in counts.most_common() if all(p in spec.split(SEP) for p in known)]
    return (fitting or [spec for spec, _ in counts.most_common()])[:MAX_OPTIONS]


# ---------- «🧩 Зібрати клас» по кроках ----------

ALL_GPUS = ["RTX 5090", "RTX 5080", "RTX 5070 Ti", "RTX 5070", "RTX 5060", "RTX 5050",
            "RTX 4090", "RTX 4080", "RTX 4070", "RTX 4060", "RTX 4050",
            "RTX 3080 Ti", "RTX 3080", "RTX 3070 Ti", "RTX 3070", "RTX 3060", "RTX 3050 Ti", "RTX 3050",
            "RTX 2080", "RTX 2070", "RTX 2060", "GTX 1660 Ti", "GTX 1650", "MX 550", "MX 450", "iGPU"]
ALL_CPUS = ([f"i{t} {g} gen" for t in (5, 7, 9) for g in range(14, 7, -1)]
            + [f"Core Ultra {t} (S{s})" for t in (5, 7, 9) for s in (2, 1)]
            + [f"Core {t} (S{s})" for t in (5, 7) for s in (2, 1)]
            + [f"Ryzen {t} {g}000" for t in (5, 7, 9) for g in (9, 8, 7, 6, 5, 4, 3)]
            + [f"Ryzen {t} 200" for t in (5, 7)] + [f"Ryzen AI {t} 300" for t in (7, 9)])
WIN_RAM = ["8GB", "16GB", "32GB+"]
MAC_CHIPS = [f"M{n}{v}" for n in (5, 4, 3, 2, 1) for v in ("", " Pro", " Max")]
MAC_SCREENS = ['13"', '14"', '15"', '16"']
MAC_RAM = ["8GB", "16GB", "18GB", "24GB", "32GB", "36GB", "48GB", "64GB", "96GB", "128GB"]


def builder_steps(watch, item):
    """Кроки «🧩 Зібрати клас»: [{'name', 'local' — варіанти з цього товару, 'all' — повний список}].
    None — оголошення не ноутбук (зібрати можна лише клас ноутбука)."""
    rows = get_current_listings(watch["id"])
    kind = item_kind(watch, item, [r["spec_group"] for r in rows])
    if kind is None:
        return None
    seen = [Counter() for _ in range(3)]
    for r in rows:
        spec = r["spec_group"] or ""
        if spec_kind(spec) != kind or spec.startswith(UNKNOWN_GPU):
            continue
        for i, part in enumerate(spec.split(SEP)[:3]):
            seen[i][part] += 1

    def step(name, i, full, check):
        local = [p for p, _ in seen[i].most_common() if check(p)][:9]
        return {"name": name, "local": local, "all": [p for p in full if p not in local] + local}

    if kind == "mac":
        return [step("чип", 0, MAC_CHIPS, lambda p: p in MAC_CHIPS),
                step("екран", 1, MAC_SCREENS, lambda p: p.endswith('"')),
                step("RAM", 2, MAC_RAM, lambda p: p in MAC_RAM)]
    return [step("відеокарта", 0, ALL_GPUS, lambda p: p != UNKNOWN_GPU),
            step("процесор", 1, ALL_CPUS, lambda p: not p.endswith("GB") and not p.endswith("GB+")),
            step("RAM", 2, WIN_RAM, lambda p: p in WIN_RAM)]


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
