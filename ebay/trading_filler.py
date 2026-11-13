"""
«Заповнювач» Trading API: вільні запити Trading (окрема квота від пошуку Browse) — на корисну
фонову роботу, щоб за добу витрачалось ~TRADING_TARGET_PCT% бюджету, а не згорало.

Витрата тягнеться рівномірно: до поточної години доби eBay (скидання о 09:00 за Берліном —
опівночі за тихоокеанським часом) має бути витрачено стільки ж відсотків від 90% бюджету.
Відстає — бот бере задачі по черзі, доки не наздожене (не більше TRADING_FILLER_BATCH за цикл):
  1. «🔥 Вигідні пропозиції»: перевірка «ще продається?» раз на 10 хв замість 30 (заодно 👁 стежать);
  2. «📉 Знизили ціну»: свіжа ціна відстежуваних оголошень — знижку видно одразу, а не після
     наступного сканування найдешевших;
  3. історія продажів: тип продавця 🏪/👤 і деталі, характеристики проданих ноутбуків.
Перевірка зниклих оголошень («продано?») завжди має пріоритет — вона працює окремо й першою.
Лише з підключеним акаунтом eBay. Викликати з потоку.
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from core.settings import (
    FILLER_DEAL_RECHECK_MINUTES,
    FILLER_TRACK_RECHECK_HOURS,
    PRICE_DROP_MIN_DAYS,
    TRADING_DAILY_BUDGET,
    TRADING_FILLER_BATCH,
    TRADING_TARGET_PCT,
    log,
)
from core.db import get_deals_to_recheck, get_tracks_to_refresh, mark_track_gone, record_api_call, track_prices
from ebay.ebay_api import trading_calls_today
from ebay.ebay_user import UserAuthError, is_connected
from ebay.trading_api import backfill_laptop_specs, backfill_seller_types, get_item_status

EBAY_DAY_TZ = ZoneInfo("America/Los_Angeles")   # ліміт eBay скидається опівночі за тихоокеанським часом
GONE = {"sold", "unsold", "not_found"}


def day_fraction(now=None):
    """Яка частина доби eBay минула (0…1)."""
    t = datetime.fromtimestamp(now or time.time(), EBAY_DAY_TZ)
    return (t.hour * 3600 + t.minute * 60 + t.second) / 86400


def trading_spare(now=None):
    """На скільки запитів витрата Trading відстає від рівномірного руху до TRADING_TARGET_PCT%."""
    target = TRADING_DAILY_BUDGET * TRADING_TARGET_PCT / 100 * day_fraction(now)
    return int(target - trading_calls_today()[0])


def refresh_deals(limit):
    """«🔥»: перевірити пропозиції, яких не перевіряли понад FILLER_DEAL_RECHECK_MINUTES. Повертає кількість."""
    from ebay.deal_check import _check   # тут, щоб не було циклу імпортів
    rows = get_deals_to_recheck(limit, FILLER_DEAL_RECHECK_MINUTES * 60)
    if rows:
        _check(rows)
    return len(rows)


def refresh_tracks(limit):
    """«📉»: свіжа ціна (з доставкою) оголошень, які вже можуть потрапити в «Знизили ціну»."""
    rows = get_tracks_to_refresh(limit, FILLER_TRACK_RECHECK_HOURS * 3600, PRICE_DROP_MIN_DAYS - 1)
    done = 0
    for r in rows:
        record_api_call("trading:markdowns")
        try:
            info = get_item_status(r["item_id"])
        except UserAuthError:
            break
        except Exception as e:
            log.debug("Не вдалося оновити ціну %s: %s", r["item_id"], e)
            continue
        done += 1
        details = info.get("details") or {}
        if info["result"] in GONE:
            mark_track_gone(r["watch_id"], r["item_id"])
        elif info["result"] == "active" and details.get("sold_price"):
            total = details["sold_price"] + (details.get("shipping_cost") or 0)
            track_prices(r["watch_id"], [{
                "item_id": r["item_id"], "title": r["title"], "url": r["url"], "cond_group": r["cond_group"],
                "spec_group": r["spec_group"], "created_at": r["listed_at"],
                "has_best_offer": r["has_best_offer"], "total_price": total}])
    return done


def run_filler(limit=TRADING_FILLER_BATCH, now=None):
    """Один цикл «заповнювача». Повертає кількість витрачених запитів."""
    if not is_connected():
        return 0
    spare = min(limit, trading_spare(now))
    if spare <= 0:
        return 0
    done = 0
    for task in (refresh_deals, refresh_tracks, backfill_seller_types, backfill_laptop_specs):
        if done >= spare:
            break
        try:
            done += task(spare - done)
        except Exception as e:   # одна задача з помилкою не зупиняє інші
            log.warning("Заповнювач Trading: %s — %s", task.__name__, e)
    if done:
        log.info("Заповнювач Trading: використано %s запитів (відставання було %s)", done, spare)
    return done
