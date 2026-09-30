"""
«🔍 Перевірити оголошення»: користувач надсилає посилання, бот завантажує
оголошення з eBay і пояснює, чи проходить воно кожен фільтр товару — і якщо
ні, то на якому саме відсіялося.
"""

import time
from datetime import datetime

from settings import (
    MIN_SELLER_FEEDBACK_PCT,
    MIN_SELLER_FEEDBACK_SCORE,
)
from textparse import (
    aspects_label,
    COMPAT_ASPECTS,
    CURRENCY_TO_EUR,
    _aspect_satisfied_by_title,
    _group_label,
    _search_tokens,
    _title_matches_search,
    condition_group_from_item,
    extract_spec_key,
    spec_key_from_aspects,
)
from db import get_market_stats, get_min_profit, get_rejected_ids, get_required_aspects, watch_category_ids
from ebay_api import fetch_item_by_legacy_id, resolve_item_id
from laptops import is_laptop, laptop_spec, laptop_warnings
from market import _stat_for_item, estimate_resale_profit, max_buy_price, watch_requires_spec

EU_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
    "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
}

OK, FAIL, INFO, WARN = "✅", "❌", "ℹ️", "⚠️"


class CheckError(Exception):
    """Зрозуміла користувачу причина, чому перевірку не вдалося виконати."""


def _money(value):
    try:
        return float((value or {}).get("value"))
    except (TypeError, ValueError):
        return None


def _parse_ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def check_listing(watch, text):
    """
    Повертає (title, url, checks, verdict), де checks — список (знак, текст).
    Кидає CheckError, якщо посилання не розпізнано чи оголошення не знайдено.
    Мережеві запити — викликати з потоку (asyncio.to_thread).
    """
    legacy_id = resolve_item_id(text)
    if not legacy_id:
        raise CheckError("Не бачу в повідомленні посилання на оголошення eBay чи його номера.")
    item = fetch_item_by_legacy_id(legacy_id)
    if item is None:
        raise CheckError("eBay не знайшов такого оголошення — можливо, його вже видалили, "
                         "або це оголошення з варіантами (кілька кольорів чи обсягів пам'яті), "
                         "яке так перевірити не вийде.")

    title = item.get("title") or ""
    url = item.get("itemWebUrl") or f"https://www.ebay.de/itm/{legacy_id}"
    checks = []

    # 1. Чи ще активне
    end_ts = _parse_ts(item.get("itemEndDate"))
    availability = [a.get("estimatedAvailabilityStatus") for a in item.get("estimatedAvailabilities") or []]
    if (end_ts and end_ts < time.time()) or "OUT_OF_STOCK" in availability:
        checks.append((FAIL, "Оголошення вже завершене або товар розпродано"))

    # 2. Формат продажу
    buying = item.get("buyingOptions") or []
    if "FIXED_PRICE" in buying:
        extra = " + можна запропонувати ціну" if "BEST_OFFER" in buying else ""
        checks.append((OK, f"Формат: «Sofort-Kaufen»{extra}"))
    else:
        checks.append((FAIL, "Аукціон — бот шукає лише «Sofort-Kaufen»"))

    # 3. Стан
    condition = item.get("condition") or "невідомий"
    cond_group = condition_group_from_item(item)
    allowed = {c.strip() for c in (watch.get("condition_ids") or "").split(",") if c.strip()}
    if cond_group == "parts":
        checks.append((FAIL, f"Стан «{condition}» — на запчастини/дефектний, такі бот не враховує"))
    elif allowed and str(item.get("conditionId") or "") not in allowed:
        checks.append((FAIL, f"Стан «{condition}» не входить у стани, які шукає цей товар"))
    else:
        checks.append((OK, f"Стан: {condition}"))

    # 4. Місце і доставка
    country = (item.get("itemLocation") or {}).get("country") or ""
    if country and country not in EU_COUNTRIES:
        checks.append((FAIL, f"Товар знаходиться поза ЄС ({country}) — бот шукає лише в ЄС"))
    shipping = [c for c in (_money(o.get("shippingCost")) for o in item.get("shippingOptions") or []) if c is not None]
    if shipping:
        cost = min(shipping)
        checks.append((OK, f"Доставка в Німеччину: {cost:.2f}€" if cost else "Доставка в Німеччину: безкоштовно"))
    else:
        checks.append((FAIL, "Немає доставки в Німеччину — схоже, лише самовивіз"))
        cost = 0.0

    # 5. Категорія
    path_ids = set(str(item.get("categoryIdPath") or item.get("categoryId") or "").split("|"))
    wanted = set(watch_category_ids(watch))
    category_name = (item.get("categoryPath") or "").replace("|", " › ")
    if wanted and not (wanted & path_ids):
        checks.append((FAIL, f"Категорія «{category_name}» не входить у вибрані категорії товару"))
    else:
        checks.append((OK, f"Категорія: {category_name or 'будь-яка'}"))

    # 6. Ціна
    price = _money(item.get("price"))
    rate = CURRENCY_TO_EUR.get(((item.get("price") or {}).get("currency") or "EUR").upper())
    total = (price + cost) * rate if price is not None and rate else None
    min_price = watch.get("min_price") or 0
    if total is None:
        checks.append((FAIL, "Не вдалося визначити ціну (незвична валюта)"))
    elif min_price and total < min_price:
        checks.append((FAIL, f"Ціна з доставкою {total:.0f}€ нижча за мінімальну ціну товару {min_price:.0f}€"))

    # 7. Назва і виключені слова
    title_tokens = _search_tokens(title)
    excluded = sorted(title_tokens & _search_tokens(watch.get("exclude") or ""))
    if excluded:
        checks.append((FAIL, "У назві є виключені слова: " + ", ".join(f"«{w}»" for w in excluded)))
    if _title_matches_search(title, watch["query"], ""):
        checks.append((OK, "Назва відповідає товару"))
    else:
        checks.append((FAIL, f"Назва не схожа на «{watch['query']}» — бот вважає це іншою моделлю чи аксесуаром"))

    # 8. Приховані вручну
    item_id = item.get("itemId")
    if item_id and item_id in get_rejected_ids(watch["id"]):
        checks.append((FAIL, "Ти раніше прибрав це оголошення кнопкою «🙈 Сховати» або «❌ Інший товар»"))

    # 9. Характеристики
    aspects = {
        (a.get("name") or "").strip().lower(): a.get("value") or ""
        for a in item.get("localizedAspects") or []
    }
    categories = [c for c in (item.get("categoryPath") or "").split("|") if c]
    laptop = is_laptop(title, categories, watch["query"])
    if laptop:
        spec = laptop_spec(title, aspects, query=watch["query"])
    else:
        spec = extract_spec_key(title)
        if spec == "unspecified":
            spec = spec_key_from_aspects(title, aspects)
    compat = [name for name in aspects if name in COMPAT_ASPECTS]
    required = get_required_aspects(watch)
    missing = [name for name in required
               if not _aspect_satisfied_by_title(name, title) and not str(aspects.get(name.lower()) or "").strip()]
    if compat:
        checks.append((FAIL, f"У характеристиках є «{compat[0]}» — так позначають аксесуари"))
    elif missing:
        checks.append((FAIL, "Не заповнені обов'язкові характеристики: " + aspects_label(missing)))
    elif not required and watch_requires_spec(watch) and spec == "unspecified":
        checks.append((FAIL, "Невідомий обсяг пам'яті — ні в назві, ні в характеристиках"))
    else:
        checks.append((OK, f"Характеристики: {spec}" if spec != "unspecified" else "Характеристики: ок"))

    # Ноутбуки: розкладка, блок живлення, BIOS… (лише попередження)
    if laptop:
        for warning in laptop_warnings(title, aspects):
            checks.append((WARN, warning.removeprefix("⚠️ ")))

    # 10. Продавець (лише попередження — такі оголошення не відкидаються)
    seller = item.get("seller") or {}
    try:
        score = int(seller.get("feedbackScore")) if seller.get("feedbackScore") is not None else None
        pct = float(seller.get("feedbackPercentage")) if seller.get("feedbackPercentage") is not None else None
    except (TypeError, ValueError):
        score = pct = None
    if score == 0:
        checks.append((WARN, "У продавця ще немає відгуків (новий акаунт) — перевір уважно"))
    elif (score is not None and score < MIN_SELLER_FEEDBACK_SCORE) or (pct is not None and pct < MIN_SELLER_FEEDBACK_PCT):
        checks.append((WARN, f"У продавця мало або погані відгуки ({score}, {pct:g}%) — перевір уважно"))

    # 11. Чи вигідне
    failed = [text for mark, text in checks if mark == FAIL]
    stats = {(s["cond_group"], s["spec_group"]): s for s in get_market_stats(watch["id"])}
    stat = _stat_for_item(stats, {"cond_group": cond_group, "spec_group": spec}) if total else None
    deal = False
    if stat:
        sale = stat["sale_price"] or stat["median_price"]
        limit = max_buy_price(sale, get_min_profit(watch["chat_id"]))
        effective = total  # вигідність — за ціною оголошення, торг лише бонус
        _, profit = estimate_resale_profit(sale, total)
        group = _group_label(stat["cond_group"], stat["spec_group"])
        if effective <= limit:
            deal = True
            checks.append((OK, f"Вигідно ({group}): {total:.0f}€ ≤ купувати до {limit:.0f}€, прибуток ~{profit:.0f}€"))
        else:
            checks.append((INFO, f"Невигідно ({group}): {total:.0f}€, а купувати варто до {limit:.0f}€"))
    elif total:
        checks.append((INFO, f"Ціни для «{_group_label(cond_group, spec)}» ще не пораховані — "
                             "замало таких оголошень у видачі"))

    if failed:
        verdict = f"❌ Відсіяно: {failed[0]}"
    elif deal:
        verdict = ("✅ Проходить усі фільтри і вигідне.\n"
                   "Якщо бот про нього не написав — воно могло не потрапити в ті 200 найновіших "
                   "оголошень, які бот бере за раз.")
    elif not stat:
        verdict = ("✅ Проходить усі фільтри, але ціни для цієї групи ще не пораховані — без них "
                   "бот не може оцінити вигідність і сповіщень не надсилає. Натисни «🔄 Оновити ціни» "
                   "в картці товару.")
    else:
        verdict = "✅ Проходить усі фільтри, але за ціною не вигідне — тому сповіщення не було."
    return title, url, checks, verdict
