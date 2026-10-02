"""
Аналітика продажів і цін для сповіщень та екранів товару:
  * як продається конфігурація — скільки продано, за якою ціною і як швидко;
  * фільтр «не йде» — конфігурації, які при живому ринку не продаються;
  * падіння цін — типова ціна групи за тиждень знизилась.
"""

import statistics
import time
from datetime import date, datetime, timedelta

from settings import (
    LOCAL_TZ,
    PRICE_DROP_ALERT_EVERY_DAYS,
    PRICE_DROP_ALERT_PCT,
    LAPTOP_SALES_WINDOW_DAYS,
    SALES_WINDOW_DAYS,
    SLOW_SELLER_MIN_WATCH_SALES,
)
from laptops import SEP as LAPTOP_SEP, spec_matches
from textparse import plural
from market import recent_median
from db import get_meta, get_price_history, set_meta


def days_to_sell(row):
    """Скільки днів оголошення висіло до продажу (None — невідомо)."""
    start = row.get("created_at") or row.get("first_seen")
    if not start or not row.get("gone_at") or row["gone_at"] < start:
        return None
    return (row["gone_at"] - start) / 86400


def window_days(sold, spec=None):
    """Вікно продажів: для ноутбуків (класи «RTX 4060 · …») — довше, вони продаються рідше."""
    specs = [spec] + [r.get("spec_group") for r in sold]
    return LAPTOP_SALES_WINDOW_DAYS if any(s and LAPTOP_SEP in s for s in specs) else SALES_WINDOW_DAYS


def group_rows(sold, cond, spec, days=None):
    """Продажі групи за останні days днів (за замовчуванням — window_days). spec «*» — усі конфігурації стану."""
    since = time.time() - (days or window_days(sold, spec)) * 86400
    return [r for r in sold if r["gone_at"] >= since and r["cond_group"] == cond
            and spec_matches(r["spec_group"], spec)]


def summarize(rows):
    """{'count', 'week', 'median_price', 'median_days'} або None, якщо продажів немає."""
    if not rows:
        return None
    week_ago = time.time() - 7 * 86400
    durations = [d for d in (days_to_sell(r) for r in rows) if d is not None]
    return {
        "count": len(rows),
        "week": sum(1 for r in rows if r["gone_at"] >= week_ago),
        "median_price": recent_median(rows),   # свіжі продажі важать більше
        "median_days": statistics.median(durations) if durations else None,
    }


def speed_text(median_days):
    if median_days is None:
        return ""
    if median_days < 1:
        return "менше ніж за добу"
    n = round(median_days)
    return f"за ~{plural(n, 'день', 'дні', 'днів')}"


def sales_note(sold, cond, spec):
    """Рядок для сповіщення: як продаються такі ж. Порожній, якщо даних немає."""
    summary = summarize(group_rows(sold, cond, spec))
    if not summary:
        return ""
    parts = [f"{plural(summary['count'], 'продаж', 'продажі', 'продажів')} за {window_days(sold, spec)} днів",
             f"типова ціна {summary['median_price']:.0f}€"]
    speed = speed_text(summary["median_days"])
    if speed:
        parts.append(f"продаються {speed}")
    return "🛒 Такі ж: " + ", ".join(parts)


def is_slow_seller(sold, cond, spec):
    """Конфігурація не продається, хоча по товару загалом продажі є — сповіщати не варто.
    Без відомої конфігурації не фільтруємо: такі оголошення розкидані по різних групах."""
    if not spec or spec in ("unspecified", "*"):
        return False
    total = len([r for r in sold if r["gone_at"] >= time.time() - window_days(sold, spec) * 86400])
    return total >= SLOW_SELLER_MIN_WATCH_SALES and not group_rows(sold, cond, spec)


def weekly_change(points, today=None):
    """Зміна типової ціни за тиждень у %: сьогоднішня точка проти найближчої
    до «7 днів тому» (6–10 днів тому). None — історії ще замало."""
    if not points:
        return None
    today = today or datetime.now(LOCAL_TZ).date()
    last_day, last_price = points[-1]
    if last_day != today.isoformat():
        return None
    window = [(d, p) for d, p in points
              if (today - timedelta(days=10)).isoformat() <= d <= (today - timedelta(days=6)).isoformat()]
    if not window:
        return None
    # найближча до рівно тижня тому
    target = (today - timedelta(days=7)).isoformat()
    _, old_price = min(window, key=lambda dp: abs(date.fromisoformat(dp[0]) - date.fromisoformat(target)))
    if not old_price:
        return None
    return (last_price - old_price) / old_price * 100, old_price, last_price


def price_drops(watch_id, today=None):
    """Групи, де типова ціна за тиждень впала на PRICE_DROP_ALERT_PCT% і більше,
    і про які ще не попереджали останні PRICE_DROP_ALERT_EVERY_DAYS днів.
    Повертає [(стан, конфігурація, стара ціна, нова ціна, %)] і позначає їх як «попереджено»."""
    drops = []
    history = get_price_history(watch_id)
    for (cond, spec), points in history.items():
        change = weekly_change(points, today)
        if not change or change[0] > -PRICE_DROP_ALERT_PCT:
            continue
        # Якщо є окремі конфігурації цього стану — про «усі» не попереджаємо (дублювало б)
        if spec == "*" and any(c == cond and s != "*" for c, s in history):
            continue
        # Ширший клас ноутбука, в якого є вужчі, — теж (попереджаємо про точний клас)
        if spec != "*" and any(c == cond and s != spec and spec_matches(s, spec) for c, s in history):
            continue
        key = f"drop_alert:{watch_id}:{cond}:{spec}"
        if time.time() - float(get_meta(key, "0") or 0) < PRICE_DROP_ALERT_EVERY_DAYS * 86400:
            continue
        set_meta(key, time.time())
        pct, old, new = change
        drops.append((cond, spec, old, new, pct))
    return drops
