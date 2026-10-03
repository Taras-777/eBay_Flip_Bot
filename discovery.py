"""
«💡 Що перепродавати»: бот сам аналізує популярні для перепродажу товари,
яких ти ще не відстежуєш, і складає рейтинг найперспективніших.

Раз на DISCOVERY_INTERVAL_HOURS для кожного кандидата — один запит до eBay
(200 найновіших оголошень). З них і зі спостережень за зниклими оголошеннями
(продані; з підключеним «🔐 Акаунт eBay» — підтверджені eBay) рахується:
  * типова ціна і реалістична ціна продажу (як для звичайних товарів);
  * скільки оголошень зараз дешевші за «купувати до» і який прибуток вони дають
    (після комісії eBay і доставки), і як часто такі з'являються;
  * продажі — скільки продано за тиждень, за якою ціною і як швидко;
    з'являються після кількох перевірок. Коли продажів достатньо, ціна продажу
    рахується за ними, а не за оголошеннями.

Список кандидатів — CANDIDATES нижче, його можна доповнювати.
"""

import re
import statistics
import time
from collections import Counter

from settings import DEFAULT_CONDITION_IDS, MIN_SOLD_SAMPLE, SEARCH_RESERVE, log
from laptops import is_laptop, laptop_spec
from textparse import _search_tokens, _title_matches_search, extract_spec_key, is_accessory_category
import config
from db import (
    get_hidden_candidates,
    get_sold_only,
    discovery_gone_stats,
    get_discovery_sold,
    get_discovery_results,
    get_meta,
    list_watches,
    save_discovery_result,
    set_meta,
    update_discovery_obs,
)
from ebay_api import browse_budget_left, search_active_items
from market import estimate_resale_profit, filter_outliers, max_buy_price, percentile
from sales import summarize

DISCOVERY_INTERVAL_HOURS = 4     # ~135 товарів × 6 разів = ~800 запитів на добу (мінус приховані)
DISCOVERY_SOLD_DAYS = 7
DISCOVERY_MIN_SAMPLE = 10         # менше оголошень — оцінка ненадійна
DISCOVERY_EXCLUDE = "broken defekt teile parts kaputt"

# (емодзі, назва, пошуковий запит, мінімальна ціна — щоб eBay не віддавав аксесуари)
# Популярні на eBay.de товари для перепродажу: ходова електроніка, яку часто
# продають вживаною і дешевше за ринок. Список можна доповнювати.
CANDIDATES = [
    # Консолі та VR
    ("🎮", "PlayStation 5 Slim", "PlayStation 5 Slim", 250),
    ("🎮", "PlayStation 5 Pro", "PlayStation 5 Pro", 450),
    ("🎮", "PlayStation Portal", "PlayStation Portal", 120),
    ("🥽", "PlayStation VR2", "PlayStation VR2", 200),
    ("🎮", "Xbox Series X", "Xbox Series X", 200),
    ("🎮", "Xbox Series S", "Xbox Series S", 120),
    ("🎮", "Nintendo Switch OLED", "Nintendo Switch OLED", 150),
    ("🎮", "Nintendo Switch 2", "Nintendo Switch 2", 250),
    ("🎮", "Nintendo Switch Lite", "Nintendo Switch Lite", 80),
    ("🎮", "Steam Deck OLED", "Steam Deck OLED", 300),
    ("🎮", "ASUS ROG Ally", "ROG Ally", 250),
    ("🎮", "Lenovo Legion Go", "Legion Go", 300),
    ("🥽", "Meta Quest 3", "Meta Quest 3", 250),
    ("🥽", "Meta Quest 3S", "Meta Quest 3S", 170),
    # Смартфони
    ("📱", "iPhone 13", "iPhone 13", 200),
    ("📱", "iPhone 13 Pro", "iPhone 13 Pro", 280),
    ("📱", "iPhone 14", "iPhone 14", 250),
    ("📱", "iPhone 14 Pro", "iPhone 14 Pro", 350),
    ("📱", "iPhone 15", "iPhone 15", 350),
    ("📱", "iPhone 15 Pro", "iPhone 15 Pro", 450),
    ("📱", "iPhone 15 Pro Max", "iPhone 15 Pro Max", 550),
    ("📱", "iPhone 16", "iPhone 16", 450),
    ("📱", "iPhone 16 Pro", "iPhone 16 Pro", 550),
    ("📱", "iPhone 16 Pro Max", "iPhone 16 Pro Max", 650),
    ("📱", "iPhone 17 Pro", "iPhone 17 Pro", 750),
    ("📱", "Samsung Galaxy S23", "Samsung Galaxy S23", 220),
    ("📱", "Samsung Galaxy S24", "Samsung Galaxy S24", 300),
    ("📱", "Samsung Galaxy S24 Ultra", "Samsung Galaxy S24 Ultra", 500),
    ("📱", "Samsung Galaxy S25", "Samsung Galaxy S25", 400),
    ("📱", "Samsung Galaxy S25 Ultra", "Samsung Galaxy S25 Ultra", 650),
    ("📱", "Samsung Galaxy Z Flip6", "Galaxy Z Flip6", 350),
    ("📱", "Google Pixel 8 Pro", "Pixel 8 Pro", 280),
    ("📱", "Google Pixel 9 Pro", "Pixel 9 Pro", 450),
    # Планшети й ноутбуки
    ("📲", "iPad Air M2", "iPad Air M2", 350),
    ("📲", "iPad Pro M4", "iPad Pro M4", 650),
    ("📲", "iPad mini 7", "iPad mini 7", 300),
    ("📲", "Samsung Galaxy Tab S9", "Galaxy Tab S9", 300),
    ("💻", "MacBook Air M1", "MacBook Air M1", 350),
    ("💻", "MacBook Air M2", "MacBook Air M2", 500),
    ("💻", "MacBook Air M3", "MacBook Air M3", 650),
    ("💻", "MacBook Pro M3", "MacBook Pro M3", 900),
    ("💻", "MacBook Pro M4", "MacBook Pro M4", 1100),
    # Ігрові й бізнесові ноутбуки: у запиті — відеокарта, бо вона найбільше впливає на ціну
    ("💻", "Lenovo Legion 5 RTX 4060", "Legion 5 RTX 4060", 600),
    ("💻", "Lenovo Legion 5 RTX 3060", "Legion 5 RTX 3060", 450),
    ("💻", "Lenovo LOQ RTX 4050", "Lenovo LOQ RTX 4050", 400),
    ("💻", "Lenovo LOQ RTX 4060", "Lenovo LOQ RTX 4060", 500),
    ("💻", "Acer Nitro 5 RTX 3050", "Acer Nitro 5 RTX 3050", 300),
    ("💻", "Acer Nitro V RTX 4050", "Acer Nitro V RTX 4050", 400),
    ("💻", "ThinkPad X1 Carbon Gen 10", "ThinkPad X1 Carbon Gen 10", 400),
    ("💻", "ThinkPad T14 Gen 3", "ThinkPad T14 Gen 3", 300),
    ("🖥", "Mac mini M4", "Mac mini M4", 400),
    # Комп'ютерні комплектуючі
    ("🖥", "RTX 4060", "RTX 4060", 180),
    ("🖥", "RTX 4070", "RTX 4070", 350),
    ("🖥", "RTX 4070 Super", "RTX 4070 Super", 420),
    ("🖥", "RTX 4080", "RTX 4080", 700),
    ("🖥", "RTX 4090", "RTX 4090", 1200),
    ("🖥", "RTX 5070", "RTX 5070", 400),
    ("🖥", "RX 7800 XT", "RX 7800 XT", 300),
    ("🖥", "Ryzen 7 7800X3D", "Ryzen 7 7800X3D", 220),
    # Аудіо
    ("🎧", "AirPods Pro 2", "AirPods Pro 2", 90),
    ("🎧", "AirPods Max", "AirPods Max", 250),
    ("🎧", "Sony WH-1000XM5", "Sony WH-1000XM5", 120),
    ("🎧", "Sony WH-1000XM4", "Sony WH-1000XM4", 90),
    ("🎧", "Bose QuietComfort Ultra", "Bose QuietComfort Ultra", 150),
    ("🔊", "Sonos Era 100", "Sonos Era 100", 120),
    ("🔊", "Sonos Arc", "Sonos Arc", 400),
    # Годинники
    ("⌚", "Apple Watch Series 9", "Apple Watch Series 9", 150),
    ("⌚", "Apple Watch Series 10", "Apple Watch Series 10", 200),
    ("⌚", "Apple Watch Ultra 2", "Apple Watch Ultra 2", 400),
    ("⌚", "Samsung Galaxy Watch 6", "Galaxy Watch 6", 90),
    ("⌚", "Garmin Fenix 7", "Garmin Fenix 7", 200),
    ("⌚", "Garmin Fenix 8", "Garmin Fenix 8", 450),
    ("⌚", "Garmin Forerunner 965", "Garmin Forerunner 965", 250),
    # Фото, відео, дрони
    ("📷", "Sony A7 III", "Sony A7 III", 700),
    ("📷", "Sony A7 IV", "Sony A7 IV", 1200),
    ("📷", "Sony ZV-E10", "Sony ZV-E10", 350),
    ("📷", "Fujifilm X100VI", "Fujifilm X100VI", 1100),
    ("📷", "Fujifilm X-T5", "Fujifilm X-T5", 900),
    ("📷", "Canon EOS R50", "Canon EOS R50", 400),
    ("📷", "Canon EOS R6", "Canon EOS R6", 1000),
    ("📷", "GoPro Hero 12", "GoPro Hero 12", 150),
    ("📷", "GoPro Hero 13", "GoPro Hero 13", 200),
    ("📷", "Insta360 X4", "Insta360 X4", 250),
    ("📷", "DJI Osmo Pocket 3", "DJI Osmo Pocket 3", 280),
    ("🚁", "DJI Mini 3 Pro", "DJI Mini 3 Pro", 350),
    ("🚁", "DJI Mini 4 Pro", "DJI Mini 4 Pro", 400),
    ("🚁", "DJI Avata 2", "DJI Avata 2", 350),
    # Побутова техніка
    ("🧹", "Dyson V15", "Dyson V15", 250),
    ("🧹", "Dyson V12", "Dyson V12", 200),
    ("💇", "Dyson Airwrap", "Dyson Airwrap", 250),
    ("💇", "Dyson Supersonic", "Dyson Supersonic", 150),
    ("🤖", "Roborock S8", "Roborock S8", 250),
    ("🍲", "Thermomix TM6", "Thermomix TM6", 600),
    ("☕", "De'Longhi Magnifica Evo", "Magnifica Evo", 200),
    ("📖", "Kindle Paperwhite", "Kindle Paperwhite", 60),
    # --- додано 3 жовтня ---
    # Консолі
    ("🎮", "Steam Deck LCD", "Steam Deck 512GB", 220),
    # Смартфони
    ("📱", "iPhone 12", "iPhone 12", 150),
    ("📱", "iPhone 12 Pro", "iPhone 12 Pro", 220),
    ("📱", "iPhone 14 Pro Max", "iPhone 14 Pro Max", 450),
    ("📱", "Samsung Galaxy S23 Ultra", "Samsung Galaxy S23 Ultra", 400),
    ("📱", "Samsung Galaxy Z Fold5", "Galaxy Z Fold5", 500),
    ("📱", "Google Pixel 8", "Pixel 8", 200),
    ("📱", "Google Pixel 9", "Pixel 9", 300),
    # Планшети й ноутбуки
    ("📲", "iPad 10", "iPad 10. Generation", 200),
    ("📲", "iPad Pro M2", "iPad Pro M2", 500),
    ("💻", "MacBook Air M4", "MacBook Air M4", 800),
    ("💻", "MacBook Pro M1 Pro", "MacBook Pro M1 Pro", 700),
    ("💻", "ASUS TUF RTX 4060", "ASUS TUF RTX 4060", 550),
    ("💻", "HP Victus RTX 4060", "HP Victus RTX 4060", 550),
    ("💻", "Lenovo Legion Pro 5 RTX 4070", "Legion Pro 5 RTX 4070", 900),
    # Комп'ютерні комплектуючі
    ("🖥", "RTX 3060", "RTX 3060", 150),
    ("🖥", "RTX 3070", "RTX 3070", 200),
    ("🖥", "RTX 3080", "RTX 3080", 300),
    ("🖥", "RTX 5070 Ti", "RTX 5070 Ti", 550),
    ("🖥", "RTX 5080", "RTX 5080", 850),
    ("🖥", "RX 7900 XTX", "RX 7900 XTX", 600),
    ("🖥", "Ryzen 7 9800X3D", "Ryzen 7 9800X3D", 350),
    # Аудіо й годинники
    ("🎧", "AirPods 4", "AirPods 4", 70),
    ("🎧", "Sony WF-1000XM5", "Sony WF-1000XM5", 100),
    ("🎧", "Bose QuietComfort 45", "Bose QuietComfort 45", 90),
    ("⌚", "Apple Watch SE 2", "Apple Watch SE 2", 90),
    ("⌚", "Samsung Galaxy Watch 7", "Galaxy Watch 7", 120),
    # Фото й дрони
    ("📷", "Sony A6400", "Sony A6400", 450),
    ("📷", "Canon EOS R7", "Canon EOS R7", 900),
    ("📷", "Nikon Z6 II", "Nikon Z6 II", 900),
    ("📷", "DJI Osmo Action 4", "DJI Osmo Action 4", 150),
    ("🚁", "DJI Mini 4K", "DJI Mini 4K", 180),
    # Побутове й транспорт
    ("🧹", "Dyson V11", "Dyson V11", 150),
    ("🤖", "Roborock S7", "Roborock S7", 180),
    ("🛴", "Segway Ninebot Max G30", "Ninebot Max G30", 250),
]


# Варіанти моделі, які рахуються окремо (Galaxy S24 ≠ S24 Ultra, RTX 4070 ≠ 4070 Ti,
# iPhone 15 ≠ 15 Pro / 15 Plus, Pixel 8 ≠ 8 Pro, Steam Deck ≠ Steam Deck OLED).
# «super» перевіряється лише після номера моделі: «super Zustand» — це не RTX Super.
EXTRA_VARIANT_TERMS = {"ultra", "ti", "fe", "pro", "max", "plus", "mini", "lite", "oled", "slim", "air"}
SUPER_MODEL = re.compile(r"\b\d{4}\s?super\b", re.IGNORECASE)


def _same_model(title, query):
    """Оголошення саме цієї моделі (не аксесуар і не сусідній варіант)."""
    if not _title_matches_search(title, query, DISCOVERY_EXCLUDE):
        return False
    title_tokens, query_tokens = _search_tokens(title), _search_tokens(query)
    for term in EXTRA_VARIANT_TERMS:
        if (term in title_tokens) != (term in query_tokens):
            return False
    return not (SUPER_MODEL.search(title) and "super" not in query_tokens)


def _model_tokens(text):
    """Слова назви моделі; «PlayStation 5» / «PS 5» → «ps5», щоб їх можна було порівнювати."""
    text = re.sub(r"play\s?station\s?(\d)", r"ps\1", (text or "").lower())
    text = re.sub(r"\bps\s(\d)", r"ps\1", text)
    return _search_tokens(text)


def _is_same_product(watch_text, candidate_query):
    """Товар, який ти відстежуєш, — це той самий кандидат «💡»: усі слова кандидата є в запиті
    (або назві) товару, і варіант той самий («iPhone 15 Pro 256GB» = «iPhone 15 Pro»,
    але не «iPhone 15 Pro Max»)."""
    watch, cand = _model_tokens(watch_text), _model_tokens(candidate_query)
    if not cand or not cand <= watch:
        return False
    return all((t in watch) == (t in cand) for t in EXTRA_VARIANT_TERMS)


def tracked_candidates(chat_id=None):
    """Назви кандидатів «💡», які користувач уже відстежує у «📦 Мої товари»."""
    watches = list_watches(chat_id=chat_id, active_only=True)
    return {name for _, name, query, _ in CANDIDATES
            if any(_is_same_product(w["query"], query) or _is_same_product(w["label"], query) for w in watches)}


def analyze_candidate(emoji, name, query, floor):
    """Один запит до eBay → метрики кандидата або None, якщо даних замало."""
    items = search_active_items(
        query=query, condition_ids=DEFAULT_CONDITION_IDS, exclude_terms=DISCOVERY_EXCLUDE,
        limit=200, min_price=floor, fresh=True,
    )
    items = [it for it in items
             if it["cond_group"] != "parts"
             and _same_model(it["title"], query)
             and not any(is_accessory_category(n) for n in it.get("category_names") or [])]
    for it in items:
        it["spec_group"] = (laptop_spec(it["title"], query=query)
                            if is_laptop(it["title"], it.get("category_names"), query) else extract_spec_key(it["title"]))

    created = [it["created_at"] for it in items if it.get("created_at")]
    update_discovery_obs(name, items, min(created) if created else None)

    # Ринок рахуємо для найпоширенішої конфігурації серед вживаних (їх перепродають найчастіше)
    used = [it for it in items if it["cond_group"] == "used"] or items
    specs = Counter(it["spec_group"] for it in used)
    spec = next((s for s, _ in specs.most_common() if s != "unspecified"), "unspecified")
    sample = [it for it in used if it["spec_group"] == spec] if specs[spec] >= DISCOVERY_MIN_SAMPLE else used
    prices = filter_outliers([it["total_price"] for it in sample])
    if len(prices) < DISCOVERY_MIN_SAMPLE:
        return None

    median = statistics.median(prices)
    sale = percentile(prices, 25)
    sale_source = "listings"

    # Продажі: спершу тієї ж конфігурації, якщо їх замало — усього стану
    sold_all = [r for r in get_discovery_sold(name, DISCOVERY_SOLD_DAYS) if r["cond_group"] != "parts"]
    cond = "used" if any(it["cond_group"] == "used" for it in items) else None
    sold = [r for r in sold_all if (cond is None or r["cond_group"] == cond)]
    same_spec = [r for r in sold if r["spec_group"] == spec]
    if len(same_spec) >= 3:
        sold = same_spec
    sold_prices = filter_outliers([r["price"] for r in sold]) if sold else []
    if len(sold_prices) >= MIN_SOLD_SAMPLE:
        sale = min(statistics.median(sold_prices), median)
        sale_source = "sold"
    buy_limit = max_buy_price(sale)
    # Вигідні — дешевші за «купувати до», але не підозріло дешеві (менше половини ціни)
    raw = [it["total_price"] for it in sample]
    deals = [p for p in raw if sale * 0.5 <= p <= buy_limit]
    deal_profit = statistics.mean(estimate_resale_profit(sale, p)[1] for p in deals) if deals else 0
    deal_share = len(deals) / len(raw) if raw else 0

    gone, observed_days = discovery_gone_stats(name)
    sold_per_day = round(gone / observed_days, 1) if observed_days >= 1 else None
    new_per_day = sum(1 for c in created if c >= time.time() - 86400)
    return {
        "emoji": emoji, "name": name, "query": query, "floor": floor,
        "spec": spec if spec != "unspecified" else None,
        "sample": len(prices), "median": round(median), "sale": round(sale),
        "buy_limit": round(buy_limit), "deals_now": len(deals), "deal_profit": round(deal_profit),
        # скільки вигідних оголошень з'являється за тиждень (частка вигідних × нові за добу × 7)
        "deals_per_week": round(deal_share * new_per_day * 7, 1),
        "sold_per_day": sold_per_day, "new_per_day": new_per_day,
        "sale_source": sale_source,
        "sold_week": len(sold),
        "sold_confirmed": sum(1 for r in sold if r["sold_check"] == "sold"),
        "sold_median": round(statistics.median(sold_prices)) if sold_prices else None,
        "sold_days": round(summarize(sold)["median_days"], 1) if sold and summarize(sold)["median_days"] is not None else None,
    }


def score(result):
    """Рейтинг: скільки можна заробити за тиждень (вигідні пропозиції × прибуток
    з кожної), з поправкою на те, як швидко товар продається."""
    weekly = result["deals_per_week"] * min(result["deal_profit"], 150)
    if result["sold_per_day"] is None:
        liquidity = 0.6  # ще не знаємо — нейтрально
    else:
        liquidity = min(1.0, 0.2 + result["sold_per_day"] / 5)
    return weekly * liquidity


def hidden_names(chat_id):
    """Товари, які користувач позначив «🙈 Не цікавить»."""
    return set(get_hidden_candidates(chat_id))


def run_discovery(force=False):
    """Аналізує кандидатів, якщо минуло DISCOVERY_INTERVAL_HOURS (або force).
    Повертає кількість проаналізованих. Викликати з потоку."""
    last = float(get_meta("discovery_at", "0") or 0)
    if not force and time.time() - last < DISCOVERY_INTERVAL_HOURS * 3600:
        return 0
    done = 0
    # Те, що власник позначив «🙈 Не цікавить», не аналізуємо — це запити до eBay
    # і те, що він уже відстежує у «📦 Мої товари» (там свої, точніші дані)
    owner = getattr(config, "OWNER_TELEGRAM_ID", 0)
    skip = (hidden_names(owner) | tracked_candidates(owner)) if owner else set()
    for emoji, name, query, floor in CANDIDATES:
        if name in skip:
            continue
        if browse_budget_left() < SEARCH_RESERVE + 50:
            log.info("«Що перепродавати»: мало запитів до eBay — решту кандидатів перевірю пізніше")
            break
        try:
            result = analyze_candidate(emoji, name, query, floor)
        except Exception as e:
            log.warning("«Що перепродавати»: не вдалося проаналізувати «%s»: %s", name, e)
            continue
        if result:
            save_discovery_result(name, result)
        done += 1
    set_meta("discovery_at", time.time())
    log.info("«Що перепродавати»: проаналізовано %s товарів", done)
    return done


def top_recommendations(chat_id=None, limit=10):
    """Найкращі кандидати, яких користувач ще не відстежує й не приховав, від найперспективнішого.
    Якщо в «⚙️ Налаштування» увімкнено «ціна лише за продажами» — лише ті, де ціна за продажами."""
    tracked = tracked_candidates(chat_id)
    hidden = hidden_names(chat_id) if chat_id is not None else set()
    sold_only = chat_id is not None and get_sold_only(chat_id)
    names = {c[1] for c in CANDIDATES}
    results = [r for r in get_discovery_results()
               if r["name"] not in tracked and r.get("deals_now")
               and r["name"] in names and r["name"] not in hidden
               and (not sold_only or r.get("sale_source") == "sold")]
    results.sort(key=score, reverse=True)
    return results[:limit] if limit else results
