"""
«🔥 Вигідні пропозиції»: чи оголошення ще продається.

Вигідні лоти розкуповують швидко, тож бот періодично перепитує eBay про кожну
пропозицію в списку. Продане або зняте оголошення зникає зі списку.
  * підключено акаунт eBay → Trading API GetItem (окремий ліміт, Browse не витрачає);
  * інакше → Browse getItem (лише поки лишається запас добового бюджету).
"""

from concurrent.futures import ThreadPoolExecutor

from settings import (
    DEAL_OPEN_RECHECK_MINUTES,
    DEAL_RECHECK_BATCH,
    DEAL_RECHECK_MINUTES,
    SEARCH_RESERVE,
    TRADING_DAILY_BUDGET,
    log,
)
from db import get_api_calls_today, get_deals_to_recheck, mark_deal_checked, record_api_call
from ebay_api import browse_budget_left, fetch_item_by_legacy_id, trading_calls_today
from ebay_user import UserAuthError, is_connected
from trading_api import get_item_status, legacy_item_id

GONE_RESULTS = {"sold", "unsold", "not_found"}


TRADING_PURPOSES = (("watch", "мої товари"), ("discovery", "💡 Що перепродавати"),
                    ("deals", "🔥 вигідні пропозиції"))


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


def listing_state(item_id):
    """(доступне, скільки стежать): доступне — True / False (продано, знято) / None (не вдалося
    дізнатися); скільки стежать — лише через Trading API, інакше None."""
    if is_connected() and trading_calls_today()[0] < TRADING_DAILY_BUDGET:
        record_api_call("trading:deals")
        try:
            info = get_item_status(item_id)
            watchers = (info.get("details") or {}).get("watch_count")
            if info["result"] == "active":
                return True, watchers
            if info["result"] in GONE_RESULTS:
                return False, watchers
        except UserAuthError:
            pass   # вхід недійсний — спробуємо через Browse
        except Exception as e:
            log.debug("Trading API не відповів про %s: %s", item_id, e)
    if browse_budget_left() <= SEARCH_RESERVE:
        return None, None   # Browse бережемо для пошуку нових лотів
    record_api_call("browse:deals")
    try:
        return fetch_item_by_legacy_id(legacy_item_id(item_id)) is not None, None
    except Exception as e:
        log.debug("Browse не відповів про %s: %s", item_id, e)
        return None, None


def listing_available(item_id):
    """True — ще продається, False — продано/знято, None — не вдалося дізнатися."""
    return listing_state(item_id)[0]


def _check(rows):
    """rows: [{'id', 'item_id'}] → скільки пропозицій прибрано як проданих/знятих."""
    removed = 0
    for row in rows:
        available, watchers = listing_state(row["item_id"])
        if available is None:
            continue
        mark_deal_checked(row["id"], gone=not available, watch_count=watchers)
        removed += not available
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
