"""
«🔥 Вигідні пропозиції»: чи оголошення ще продається.

Вигідні лоти розкуповують швидко, тож бот періодично перепитує eBay про кожну
пропозицію в списку. Продане або зняте оголошення зникає зі списку. Заразом бот бере свіжу ціну:
продавець підняв ціну так, що вже невигідно, або в «аукціон + купити зараз» з'явилась ставка і
«купити зараз» зникло, — пропозиція теж зникає; ціна знизилась — показується нова.
  * підключено акаунт eBay → Trading API GetItem (окремий ліміт, Browse не витрачає);
  * інакше → Browse getItem (лише поки лишається запас добового бюджету).
"""

from concurrent.futures import ThreadPoolExecutor

from core import settings

from core.settings import (
    DEAL_OPEN_RECHECK_MINUTES,
    DEAL_RECHECK_BATCH,
    DEAL_RECHECK_MINUTES,
    SEARCH_RESERVE,
    TRADING_DAILY_BUDGET,
    log,
)
from core.db import (
    get_api_calls_today,
    get_deals_to_recheck,
    mark_deal_checked,
    min_profit_rules,
    profit_needed,
    record_api_call,
    set_deal_total_price,
)
from ebay.ebay_api import browse_buy_now_total, browse_budget_left, fetch_item_by_legacy_id, trading_calls_today
from ebay.ebay_user import UserAuthError, is_connected
from ebay.trading_api import get_item_status, legacy_item_id

GONE_RESULTS = {"sold", "unsold", "not_found"}


TRADING_PURPOSES = (("watch", "мої товари"), ("discovery", "💡 Що перепродавати"),
                    ("deals", "🔥 вигідні пропозиції"), ("specs", "характеристики"), ("markdowns", "📉 знижки"),
                    ("auctions", "🔨 аукціони"), ("export", "📤 вивантаження"))


def trading_breakdown_lines():
    """Рядки для «⚙️ Налаштування»: на що сьогодні пішли запити Trading API."""
    total = trading_calls_today()[0]
    parts = [(label, get_api_calls_today(f"trading:{key}")) for key, label in TRADING_PURPOSES]
    lines = ["   " + " · ".join(f"{label}: {n}" for label, n in parts)]
    other = total - sum(n for _, n in parts)
    if other > 0:
        lines.append(f"   інше: {other} (до оновлення бота, копія бота на ПК або check_sold.py)")
    browse = get_api_calls_today("browse:deals")
    if browse:
        lines.append(f"   🔥 вигідні пропозиції через Browse API: {browse}")
    return lines


PRICE_CHANGE_EUR = 1   # меншу різницю (округлення, курс) не вважаємо зміною ціни


def _trading_offer(details):
    """Ціна «купити зараз» з доставкою з відповіді Trading: {"total": €} / {"bin_gone": True} / None."""
    ship = details.get("shipping_cost")
    if details.get("listing_type") == "Chinese":   # аукціон: купити можна лише за «купити зараз»
        if details.get("bin_available") is False or not details.get("buy_it_now"):
            return {"bin_gone": True}
        price = details["buy_it_now"]
    else:
        price = details.get("sold_price")   # для «купити зараз» — поточна ціна оголошення
    if price is None or ship is None:
        return None
    return {"total": round(price + ship, 2)}


def listing_state(item_id):
    """(доступне, скільки стежать, ціна): доступне — True / False (продано, знято) / None (не вдалося
    дізнатися); скільки стежать — лише через Trading API, інакше None; ціна — {"total": € з доставкою},
    {"bin_gone": True} (у аукціоні вже не можна купити зараз) або None (невідомо)."""
    if is_connected() and trading_calls_today()[0] < TRADING_DAILY_BUDGET:
        record_api_call("trading:deals")
        try:
            info = get_item_status(item_id)
            details = info.get("details") or {}
            watchers = details.get("watch_count")
            if info["result"] == "active":
                return True, watchers, _trading_offer(details)
            if info["result"] in GONE_RESULTS:
                return False, watchers, None
        except UserAuthError:
            pass   # вхід недійсний — спробуємо через Browse
        except Exception as e:
            log.debug("Trading API не відповів про %s: %s", item_id, e)
    if browse_budget_left() <= SEARCH_RESERVE:
        return None, None, None   # Browse бережемо для пошуку нових лотів
    record_api_call("browse:deals")
    try:
        data = fetch_item_by_legacy_id(legacy_item_id(item_id))
    except Exception as e:
        log.debug("Browse не відповів про %s: %s", item_id, e)
        return None, None, None
    if data is None:
        return False, None, None
    total = browse_buy_now_total(data)
    return True, None, ({"total": total} if total is not None else {"bin_gone": True})


def listing_available(item_id):
    """True — ще продається, False — продано/знято, None — не вдалося дізнатися."""
    return listing_state(item_id)[0]


def _check(rows):
    """rows: [{'id', 'item_id', 'watch_id', 'total_price', 'median_price'}] → скільки пропозицій прибрано
    зі списку: продано / знято / «купити зараз» зникло / подорожчало так, що вже невигідно."""
    removed = 0
    rules = None
    for row in rows:
        state = listing_state(row["item_id"])
        available, watchers = state[0], state[1]
        offer = state[2] if len(state) > 2 else None
        if available is None:
            continue
        if available and offer and offer.get("bin_gone"):
            available = False   # аукціон зі ставкою — купити за ціною знахідки вже не можна
        mark_deal_checked(row["id"], gone=not available, watch_count=watchers)
        if not available:
            removed += 1
            continue
        new_total, old_total = (offer or {}).get("total"), row.get("total_price")
        if new_total is None or old_total is None or abs(new_total - old_total) < PRICE_CHANGE_EUR:
            continue
        set_deal_total_price(row["id"], new_total)
        if rules is None:
            rules = min_profit_rules()
        sale = row.get("median_price") or 0
        rule = rules.get(row.get("watch_id"), ("eur", 0))
        net = sale * (1 - settings.EBAY_SELLING_FEES_PCT / 100) - settings.RESALE_SHIPPING_EUR
        if net - new_total < profit_needed(rule, sale):
            removed += 1   # подорожчало — зі списку зникає саме (фільтр мін. прибутку)
    return removed


def recheck_deals(limit=DEAL_RECHECK_BATCH):
    """Фонова перевірка: пропозиції, які не перевіряли понад DEAL_RECHECK_MINUTES.
    Викликати з потоку. Повертає кількість прибраних."""
    rows = get_deals_to_recheck(limit, DEAL_RECHECK_MINUTES * 60)
    removed = _check(rows)
    if rows:
        log.info("Перевірено вигідних пропозицій: %s, прибрано проданих/знятих: %s", len(rows), removed)
    return removed


def recheck_shown_deals(deal_ids):
    """Сторінка, яку користувач відкриває: перевіряємо паралельно ті, що перевіряли
    давніше за DEAL_OPEN_RECHECK_MINUTES. Викликати з потоку. Повертає кількість прибраних."""
    rows = get_deals_to_recheck(len(deal_ids), DEAL_OPEN_RECHECK_MINUTES * 60, deal_ids=deal_ids)
    if not rows:
        return 0
    with ThreadPoolExecutor(max_workers=min(5, len(rows))) as pool:
        return sum(pool.map(lambda r: _check([r]), rows))
