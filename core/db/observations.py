"""
Спостереження за оголошеннями: зниклі (ймовірно продані), перевірка продажів, тип продавця.
"""

import json
import time
from datetime import datetime, timezone

from core.settings import (
    CATEGORY_ASPECTS_CACHE_DAYS,
    GONE_MAX_LISTING_DAYS,
    GONE_MISS_THRESHOLD,
    LOCAL_TZ,
    LISTING_OBS_RETENTION_DAYS,
    MARKET_REFRESH_MINUTES,
    PRICE_HISTORY_DAYS,
    SOLD_CHECK_MAX_AGE_DAYS,
    SOLD_LOOKBACK_DAYS,
)


# ---------- спостереження за лотами ("зниклі" = ймовірно продані) ----------

def update_listing_observations(watch_id, items, window_start=None, present_ids=None):
    """
    Оновлює спостереження після ринкового сканування (N найновіших лотів,
    sort=newlyListed). Лот, створений ПІЗНІШЕ за найстаріший лот поточної
    видачі, мав би в ній бути; якщо його немає GONE_MISS_THRESHOLD разів
    поспіль — він зник (ймовірно куплений). Лоти, старші за вікно
    видачі, не оцінюємо: вони могли просто вийти за межі N найновіших.
    """
    now = int(time.time())
    # Присутні — усі лоти видачі (зокрема тимчасово відфільтровані), щоб не
    # прийняти за "зниклий" лот, який просто не пройшов фільтр цього разу
    present = set(present_ids) if present_ids else {it["item_id"] for it in items if it.get("item_id")}
    if window_start is None:
        created = [it["created_at"] for it in items if it.get("created_at")]
        window_start = min(created) if created else None

    with get_conn() as conn:
        known = {r["item_id"] for r in conn.execute(
            "SELECT item_id FROM listing_obs WHERE watch_id = ?", (watch_id,)).fetchall()}
        new_count = sum(1 for it in items if it.get("item_id") and it["item_id"] not in known)
        for it in items:
            if not it.get("item_id"):
                continue
            conn.execute(
                """INSERT INTO listing_obs (watch_id, item_id, cond_group, spec_group, price,
                                            created_at, end_at, first_seen, last_seen, miss_count, status,
                                            title, url, category_id, category_name, buying_options,
                                            condition_id, shipping_cost, pickup_only, country, current_bid, bid_count,
                                            seller_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(watch_id, item_id) DO UPDATE SET
                     cond_group=excluded.cond_group, spec_group=excluded.spec_group,
                     price=excluded.price, end_at=excluded.end_at, last_seen=excluded.last_seen,
                     miss_count=0, status='active', gone_at=NULL, sold_check=NULL,
                     title=excluded.title, url=excluded.url,
                     category_id=COALESCE(excluded.category_id, listing_obs.category_id),
                     category_name=COALESCE(excluded.category_name, listing_obs.category_name),
                     buying_options=COALESCE(excluded.buying_options, listing_obs.buying_options),
                     condition_id=COALESCE(excluded.condition_id, listing_obs.condition_id),
                     shipping_cost=COALESCE(excluded.shipping_cost, listing_obs.shipping_cost),
                     pickup_only=COALESCE(excluded.pickup_only, listing_obs.pickup_only),
                     country=COALESCE(excluded.country, listing_obs.country),
                     current_bid=excluded.current_bid,
                     bid_count=COALESCE(excluded.bid_count, listing_obs.bid_count),
                     seller_type=COALESCE(excluded.seller_type, listing_obs.seller_type)""",
                (watch_id, it["item_id"], it["cond_group"], it.get("spec_group", "unspecified"),
                 it["total_price"], it.get("created_at"), it.get("end_at"), now, now,
                 it.get("title"), it.get("url"), it.get("category_id"), it.get("category_name"),
                 it.get("buying_options"), it.get("condition_id") or None, it.get("shipping_cost"),
                 None if it.get("pickup_only") is None else int(it["pickup_only"]), it.get("country"),
                 it.get("current_bid"), it.get("bid_count"), it.get("seller_type")),
            )

        if window_start is None:
            return new_count
        rows = conn.execute(
            """SELECT item_id, miss_count, end_at, first_seen, created_at, buying_options FROM listing_obs
               WHERE watch_id = ? AND status = 'active' AND created_at IS NOT NULL AND created_at >= ?""",
            (watch_id, window_start),
        ).fetchall()
        for r in rows:
            if r["item_id"] in present:
                continue
            misses = r["miss_count"] + 1
            if r["end_at"] and r["end_at"] <= now:
                status = "ended"  # закінчився строк оголошення — це не продаж
            elif "AUCTION" in (r["buying_options"] or "") and misses >= GONE_MISS_THRESHOLD:
                status = "ended"  # «аукціон + купити зараз»: невідомо, за якою ціною — не продаж для статистики
            elif misses >= GONE_MISS_THRESHOLD:
                age_days = (now - (r["created_at"] or r["first_seen"])) / 86400
                status = "gone" if age_days <= GONE_MAX_LISTING_DAYS else "ended"
            else:
                status = "active"
            # Зниклий лот ставимо в чергу на перевірку «справді продано?» (Trading API)
            # Аукціон на перевірку «продано?» не ставимо — у статистику він однаково не йде
            check = "active" if "AUCTION" in (r["buying_options"] or "") else status
            conn.execute(
                """UPDATE listing_obs SET miss_count = ?, status = ?,
                     gone_at = CASE WHEN ? != 'active' THEN ? ELSE gone_at END,
                     sold_check = CASE WHEN ? != 'active' THEN 'pending' ELSE sold_check END
                   WHERE watch_id = ? AND item_id = ?""",
                (misses, status, status, now, check, watch_id, r["item_id"]),
            )
    return new_count


# Результат перевірки Trading API → (новий статус лота, позначка перевірки).
# None — статус не змінюємо (eBay не дав відповіді — лишається оцінка за зникненням).
SOLD_CHECK_OUTCOMES = {
    "sold": ("gone", "sold"),        # справді продано — ціна йде в статистику
    "unsold": ("ended", "unsold"),   # продавець зняв без продажу — не рахуємо
    "active": ("ended", "active"),   # ще продається, просто випало з видачі — не рахуємо
    "not_found": (None, "unknown"),
    "unknown": (None, "unknown"),
}


def sold_confirmed_today(since=None):
    """Скільки перевірок показали «продано» з моменту since (unix-час; за замовчуванням — з початку
    доби UTC): твої товари + «💡 Що перепродавати»."""
    start = since if since is not None else \
        int(datetime.strptime(_utc_day(), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    with get_conn() as conn:
        return sum(conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE sold_check = 'sold' AND checked_at >= ?", (start,)
        ).fetchone()["c"] for table in ("listing_obs", "discovery_obs"))


def get_pending_sold_checks(limit, watch_id=None):
    """Зниклі лоти, які ще треба перевірити через Trading API (найсвіжіші першими)."""
    since = int(time.time()) - SOLD_CHECK_MAX_AGE_DAYS * 86400
    only = " AND watch_id = ?" if watch_id is not None else ""
    params = (since,) + ((watch_id,) if watch_id is not None else ()) + (limit,)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT watch_id, item_id, price, status FROM listing_obs
               WHERE sold_check = 'pending' AND gone_at >= ?
               AND watch_id NOT IN (SELECT id FROM watches WHERE active = 0)""" + only
            + " ORDER BY gone_at DESC LIMIT ?",
            params,
        ).fetchall()]


SALE_DETAIL_FIELDS = ("sold_price", "sold_at", "bid_count", "offer_count", "quantity", "listing_type",
                      "watch_count")


def apply_sold_check(watch_id, item_id, result, details=None):
    """details — деталі завершення з GetItem (лише для історії). Якщо їх немає (лот уже
    перевіряли для іншого товару), беремо з того запису."""
    status, mark = SOLD_CHECK_OUTCOMES.get(result, (None, "unknown"))
    with get_conn() as conn:
        updated = conn.execute(
            """UPDATE listing_obs SET sold_check = ?, checked_at = ?, status = COALESCE(?, status)
               WHERE watch_id = ? AND item_id = ? AND sold_check = 'pending'""",
            (mark, int(time.time()), status, watch_id, item_id),
        ).rowcount
        if not updated:
            return
        if details is None:
            row = conn.execute(
                "SELECT " + ", ".join(SALE_DETAIL_FIELDS) + " FROM listing_obs WHERE item_id = ? "
                "AND watch_id != ? AND sold_check IS NOT NULL AND sold_check != 'pending' LIMIT 1",
                (item_id, watch_id)).fetchone()
            details = dict(row) if row else {}
        values = [details.get(f) for f in SALE_DETAIL_FIELDS]
        if any(v is not None for v in values):
            conn.execute("UPDATE listing_obs SET " + ", ".join(f"{f} = ?" for f in SALE_DETAIL_FIELDS)
                         + " WHERE watch_id = ? AND item_id = ?", (*values, watch_id, item_id))


def sold_check_stats(days=7):
    """Скільки зниклих лотів перевірено за останні дні: {позначка: кількість}."""
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT sold_check, COUNT(*) AS c FROM listing_obs
               WHERE sold_check IS NOT NULL AND gone_at >= ? GROUP BY sold_check""",
            (since,),
        ).fetchall()
    return {r["sold_check"]: r["c"] for r in rows}


def get_confirmed_only():
    """«Лише підтверджені продажі»: при підключеному акаунті eBay продажем вважається лише те, що eBay
    підтвердив (✅). Без цього — ще й зниклі оголошення, про які eBay не відповів (оцінка за зникненням)."""
    return get_meta("confirmed_sales_only") == "1"


def set_confirmed_only(value):
    """Перемкнути й перерахувати ціни всіх товарів (продажі для статистики тепер інші)."""
    set_meta("confirmed_sales_only", "1" if value else "0")
    with get_conn() as conn:
        conn.execute("UPDATE market_stats SET updated_at = 0")


def _sold_filter():
    """Коли перевірка продажів через eBay увімкнена (є вхід в акаунт), лоти з ⏳
    ще не є продажами — у статистику йдуть лише після відповіді eBay. З «лише підтверджені» —
    лише ті, продаж яких eBay підтвердив."""
    if not get_meta("ebay_user_refresh_token"):
        return ""
    return " AND sold_check = 'sold'" if get_confirmed_only() else " AND COALESCE(sold_check, '') != 'pending'"


def get_gone_rows(watch_id, cond_group, spec_group=None):
    """Ціни й час зникнення лотів за останні SOLD_LOOKBACK_DAYS днів → [{'price', 'gone_at'}]."""
    since = int(time.time()) - SOLD_LOOKBACK_DAYS * 86400
    q = ("SELECT price, gone_at, condition_id FROM listing_obs WHERE watch_id = ? AND status = 'gone' AND gone_at >= ? "
         "AND cond_group = ?" + _sold_filter())
    params = [watch_id, since, cond_group]
    if spec_group is not None:
        # Ширша група («RTX 4060», «500GB») включає вужчі («RTX 4060 · i7 13 gen», «500GB · Slim»)
        q += " AND (spec_group = ? OR spec_group LIKE ? ESCAPE '\\')"
        like = spec_group.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + " · %"
        params += [spec_group, like]
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall() if r["price"]]


def get_gone_prices(watch_id, cond_group, spec_group=None):
    """Ціни лотів, які зникли з видачі за останні SOLD_LOOKBACK_DAYS днів."""
    return [r["price"] for r in get_gone_rows(watch_id, cond_group, spec_group)]


def get_sold_listings(watch_id, days=SOLD_LOOKBACK_DAYS):
    """Продані (зниклі до кінця строку або підтверджені eBay) лоти товару, найновіші першими.
    Лоти, що ще чекають перевірки eBay (⏳), не враховуються, якщо перевірка увімкнена."""
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT item_id, cond_group, spec_group, price, created_at, first_seen, gone_at,
                      title, url, sold_check, buying_options, offer_count
               FROM listing_obs WHERE watch_id = ? AND status = 'gone' AND gone_at >= ? AND price IS NOT NULL"""
            + _sold_filter() + " ORDER BY gone_at DESC",
            (watch_id, since),
        ).fetchall()]


def active_listing_counts(watch_id):
    """Скільки оголошень зараз у продажу (бот їх відстежує) — за станом і конфігурацією."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT cond_group, COALESCE(spec_group, 'unspecified') AS spec, COUNT(*) AS c
               FROM listing_obs WHERE watch_id = ? AND status = 'active' GROUP BY cond_group, spec""",
            (watch_id,),
        ).fetchall()
    return {(r["cond_group"], r["spec"]): r["c"] for r in rows}


def watch_obs_summary(watch_id, days=SOLD_LOOKBACK_DAYS):
    """Скільки оголошень бот відстежує по товару, скільки з них продано (за `days` днів),
    скільки чекає перевірки «продано?» і скільки знято без продажу."""
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        row = conn.execute(
            """SELECT
                 SUM(status = 'active') AS active,
                 SUM(status = 'gone' AND gone_at >= :since AND (:verify = 0 OR COALESCE(sold_check, '') != 'pending')
                     AND (:confirmed = 0 OR sold_check = 'sold')) AS sold,
                 SUM(status = 'gone' AND gone_at >= :since AND sold_check = 'sold') AS confirmed,
                 SUM(sold_check = 'pending') AS pending,
                 SUM(sold_check IN ('unsold', 'active') AND gone_at >= :since) AS withdrawn
               FROM listing_obs WHERE watch_id = :wid""",
            {"since": since, "wid": watch_id, "verify": 1 if get_meta("ebay_user_refresh_token") else 0,
             "confirmed": 1 if get_meta("ebay_user_refresh_token") and get_confirmed_only() else 0},
        ).fetchone()
    return {k: int(row[k] or 0) for k in ("active", "sold", "confirmed", "pending", "withdrawn")}


def copy_listing_history(src_watch_ids, dst_watch_id, allowed_conditions=None, exclude_tokens=(), min_price=0,
                         gone_only_from=()):
    """Копіює спостереження (активні й продані лоти) з товарів-«сусідів» новому товару,
    з урахуванням його фільтрів. gone_only_from — товари, з яких беремо лише зниклі/продані (напр. з архіву:
    їхні «поточні» оголошення застаріли). Повертає кількість скопійованих."""
    from analysis.textparse import _search_tokens
    if not src_watch_ids:
        return 0
    exclude = set(exclude_tokens or ())
    marks = ",".join("?" * len(src_watch_ids))
    with get_conn() as conn:
        rejected = {r["item_id"] for r in conn.execute(
            "SELECT item_id FROM rejected_items WHERE watch_id = ?", (dst_watch_id,)).fetchall()}
        rows = conn.execute(
            f"""SELECT * FROM listing_obs WHERE watch_id IN ({marks})
                ORDER BY COALESCE(sold_check = 'sold', 0) DESC, last_seen DESC""",
            list(src_watch_ids),
        ).fetchall()
        copied = 0
        for r in rows:
            r = dict(r)
            if r["item_id"] in rejected:
                continue
            if r["watch_id"] in gone_only_from and r["status"] == "active":
                continue
            # Стани товару (коди eBay) — група тепер «магазин/приватний», тож фільтруємо за кодом стану
            if allowed_conditions and r["condition_id"] and str(r["condition_id"]) not in allowed_conditions:
                continue
            if exclude and _search_tokens(r.get("title") or "") & exclude:
                continue
            if min_price and (r["price"] or 0) < min_price:
                continue
            r["watch_id"] = dst_watch_id
            cols = list(r)
            cur = conn.execute(
                f"INSERT OR IGNORE INTO listing_obs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [r[c] for c in cols],
            )
            copied += cur.rowcount
    return copied


def known_sold_check(item_id):
    """Результат перевірки «продано?», якщо цей лот уже перевіряли в іншому товарі
    чи в «Що перепродавати», — щоб не питати eBay двічі."""
    with get_conn() as conn:
        for table in ("listing_obs", "discovery_obs"):
            row = conn.execute(
                f"""SELECT sold_check FROM {table} WHERE item_id = ?
                    AND sold_check IN ('sold', 'unsold', 'active') LIMIT 1""",
                (item_id,),
            ).fetchone()
            if row:
                return row["sold_check"]
    return None


def get_current_listings(watch_id):
    """Активні оголошення, які бот бачив в останніх ринкових скануваннях."""
    since = int(time.time()) - 2 * MARKET_REFRESH_MINUTES * 60
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT item_id, cond_group, spec_group, price, title, url, buying_options, current_bid,
                      bid_count, end_at, pickup_only FROM listing_obs
               WHERE watch_id = ? AND status = 'active' AND last_seen >= ? AND price IS NOT NULL""",
            (watch_id, since),
        ).fetchall()]


def get_spec_rows(title_word=None):
    """Записи історії (у назві яких є слово, або всі) — для переформатування конфігурацій."""
    sql = "SELECT watch_id, item_id, title, spec_group FROM listing_obs WHERE spec_group IS NOT NULL"
    params = []
    if title_word:
        sql += " AND LOWER(title) LIKE ?"
        params.append(f"%{title_word.lower()}%")
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_spec_rows_for_items(item_ids):
    ids = [i for i in item_ids if i]
    result = []
    with get_conn() as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            result += [dict(r) for r in conn.execute(
                "SELECT watch_id, item_id, title, spec_group FROM listing_obs WHERE item_id IN ("
                + ",".join("?" * len(chunk)) + ")", chunk).fetchall()]
    return result


def set_listing_spec(watch_id, item_id, spec_group):
    with get_conn() as conn:
        conn.execute("UPDATE listing_obs SET spec_group = ? WHERE watch_id = ? AND item_id = ?",
                     (spec_group, watch_id, item_id))
        conn.execute("UPDATE item_specs SET spec_group = ? WHERE item_id = ?", (spec_group, item_id))


def get_all_listing_rows(watch_id):
    """Усі спостереження товару (активні, зниклі, продані) — id і назва, для чистки історії."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT item_id, title FROM listing_obs WHERE watch_id = ?", (watch_id,)).fetchall()]


def record_scan_stats(watch_id, fetched, kept, new_count):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO scan_stats (watch_id, scanned_at, fetched, kept, new_count) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(watch_id) DO UPDATE SET scanned_at = excluded.scanned_at,
                 fetched = excluded.fetched, kept = excluded.kept, new_count = excluded.new_count""",
            (watch_id, int(time.time()), fetched, kept, new_count),
        )


def get_scan_summary(chat_id):
    """Підсумок останніх ринкових сканувань усіх активних товарів чату."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS watches, SUM(s.fetched) AS fetched, SUM(s.kept) AS kept,
                      SUM(s.new_count) AS new_count, MAX(s.scanned_at) AS last
               FROM scan_stats s JOIN watches w ON w.id = s.watch_id
               WHERE w.chat_id = ? AND w.active = 1""",
            (chat_id,),
        ).fetchone()
    if not row or not row["watches"]:
        return None
    return {k: int(row[k] or 0) for k in ("watches", "fetched", "kept", "new_count", "last")}


def record_price_history(watch_id, stats):
    """Знімок типових цін груп за сьогодні (оновлюється протягом дня)."""
    day = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    with get_conn() as conn:
        for s in stats:
            conn.execute(
                """INSERT INTO price_history (watch_id, cond_group, spec_group, day, median_price, sale_price)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(watch_id, cond_group, spec_group, day) DO UPDATE SET
                     median_price=excluded.median_price, sale_price=excluded.sale_price""",
                (watch_id, s["cond_group"], s["spec_group"], day, s["median_price"], s.get("sale_price")),
            )


def get_price_history(watch_id):
    """{(стан, конфігурація): [(день, типова ціна), ...]} від найстаріших."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT cond_group, spec_group, day, median_price FROM price_history
               WHERE watch_id = ? ORDER BY day""", (watch_id,)).fetchall()
    history = {}
    for r in rows:
        history.setdefault((r["cond_group"], r["spec_group"]), []).append((r["day"], r["median_price"]))
    return history


def cleanup_old_listing_obs():
    cutoff = int(time.time()) - LISTING_OBS_RETENTION_DAYS * 86400
    with get_conn() as conn:
        old_day = datetime.fromtimestamp(time.time() - PRICE_HISTORY_DAYS * 86400, LOCAL_TZ).strftime("%Y-%m-%d")
        conn.execute("DELETE FROM price_history WHERE day < ?", (old_day,))
        conn.execute("DELETE FROM item_specs WHERE fetched_at < ?", (cutoff,))
        conn.execute("DELETE FROM price_track WHERE last_seen < ?", (cutoff,))
        return conn.execute("DELETE FROM listing_obs WHERE last_seen < ?", (cutoff,)).rowcount


def get_cached_specs(item_ids):
    """item_id → (spec_group, aspects dict або None, якщо характеристики не збережені)."""
    if not item_ids:
        return {}
    with get_conn() as conn:
        placeholders = ",".join("?" * len(item_ids))
        rows = conn.execute(
            f"SELECT item_id, spec_group, aspects_json FROM item_specs WHERE item_id IN ({placeholders})",
            list(item_ids),
        ).fetchall()
    result = {}
    for r in rows:
        try:
            aspects = json.loads(r["aspects_json"]) if r["aspects_json"] else None
        except ValueError:
            aspects = None
        result[r["item_id"]] = (r["spec_group"], aspects)
    return result


def save_cached_spec(item_id, spec_group, aspects=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO item_specs (item_id, spec_group, fetched_at, aspects_json) VALUES (?, ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET spec_group=excluded.spec_group,
                 fetched_at=excluded.fetched_at, aspects_json=excluded.aspects_json""",
            (item_id, spec_group, int(time.time()),
             json.dumps(aspects, ensure_ascii=False) if aspects is not None else None),
        )


def save_item_availability(item_id, sold, available):
    """«Продано N штук» з характеристик оголошення — лише для історії."""
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO item_specs (item_id, spec_group, fetched_at, est_sold, est_available, availability_at)
               VALUES (?, 'unspecified', ?, ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET est_sold = excluded.est_sold,
                 est_available = excluded.est_available, availability_at = excluded.availability_at""",
            (item_id, now, sold, available, now))


def save_item_aspects(item_id, aspects):
    """Характеристики з Trading API — у кеш, якщо їх там ще немає (порожні — щоб більше не питати)."""
    data = json.dumps(aspects or {}, ensure_ascii=False)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO item_specs (item_id, spec_group, fetched_at, aspects_json) VALUES (?, 'unspecified', ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET aspects_json = CASE
                 WHEN item_specs.aspects_json IS NULL OR item_specs.aspects_json = '{}' THEN excluded.aspects_json
                 ELSE item_specs.aspects_json END""",
            (item_id, int(time.time()), data))


def get_gone_without_aspects(watch_ids, max_age_days):
    """Зниклі лоти товарів без збережених характеристик → [{'watch_id', 'item_id', 'spec_group'}], свіжі першими."""
    ids = list(watch_ids)
    if not ids:
        return []
    since = int(time.time()) - max_age_days * 86400
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT o.watch_id, o.item_id, o.spec_group FROM listing_obs o "
            "LEFT JOIN item_specs s ON s.item_id = o.item_id "
            "WHERE o.status = 'gone' AND o.gone_at >= ? AND s.aspects_json IS NULL AND o.watch_id IN ("
            + ",".join("?" * len(ids)) + ") ORDER BY o.gone_at DESC", [since, *ids]).fetchall()]


def get_bin_prices(item_ids):
    """Збережені ціни «купити зараз» → {item_id: ціна}."""
    ids = [i for i in item_ids if i]
    if not ids:
        return {}
    with get_conn() as conn:
        rows = conn.execute("SELECT item_id, bin_price FROM item_specs WHERE bin_price IS NOT NULL AND item_id IN ("
                            + ",".join("?" * len(ids)) + ")", ids).fetchall()
    return {r["item_id"]: r["bin_price"] for r in rows}


def save_bin_price(item_id, price):
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO item_specs (item_id, spec_group, fetched_at, bin_price) VALUES (?, 'unspecified', ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET bin_price = excluded.bin_price""", (item_id, now, price))


def get_cached_category_aspects(category_id):
    since = int(time.time()) - CATEGORY_ASPECTS_CACHE_DAYS * 86400
    with get_conn() as conn:
        row = conn.execute(
            "SELECT aspects_json FROM category_aspects WHERE category_id = ? AND fetched_at >= ?",
            (str(category_id), since),
        ).fetchone()
    return json.loads(row["aspects_json"]) if row else None


def save_category_aspects(category_id, aspects):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO category_aspects (category_id, aspects_json, fetched_at) VALUES (?, ?, ?)
               ON CONFLICT(category_id) DO UPDATE SET aspects_json=excluded.aspects_json,
                 fetched_at=excluded.fetched_at""",
            (str(category_id), json.dumps(aspects, ensure_ascii=False), int(time.time())),
        )


# ---------- 🏪 магазин / 👤 приватний ----------

def set_seller_type(item_id, seller_type):
    """Тип продавця з Trading API ('business' / 'individual' / 'unknown' — eBay не віддав) — в історію
    всіх товарів з цим оголошенням; група магазин/приватний — відповідно (запчастини не чіпаємо).
    Повертає товари, де група змінилась (їм перерахувати ринок)."""
    group = {"business": "new", "individual": "used"}.get(seller_type)
    with get_conn() as conn:
        changed = [r["watch_id"] for r in conn.execute(
            "SELECT DISTINCT watch_id FROM listing_obs WHERE item_id = ? AND cond_group NOT IN ('parts', ?)",
            (item_id, group)).fetchall()] if group else []
        if group:
            conn.execute("UPDATE listing_obs SET seller_type = ? WHERE item_id = ?", (seller_type, item_id))
        else:   # «eBay не віддав» не затирає тип, відомий з пошуку
            conn.execute("UPDATE listing_obs SET seller_type = ? WHERE item_id = ? AND seller_type IS NULL",
                         (seller_type, item_id))
        if group:
            conn.execute("UPDATE listing_obs SET cond_group = ? WHERE item_id = ? AND cond_group != 'parts'",
                         (group, item_id))
    return changed


def get_sold_without_seller_type(max_age_days, limit):
    """Продані (зниклі) оголошення без типу продавця, які eBay ще віддає (≤ max_age_days), свіжі першими."""
    if limit <= 0:
        return []
    since = int(time.time()) - max_age_days * 86400
    with get_conn() as conn:
        return [r["item_id"] for r in conn.execute(
            """SELECT item_id, MAX(gone_at) AS g FROM listing_obs
               WHERE status = 'gone' AND gone_at >= ? AND seller_type IS NULL
                 AND COALESCE(sold_check, '') NOT IN ('pending', 'unsold')
               GROUP BY item_id ORDER BY g DESC LIMIT ?""", (since, limit)).fetchall()]


def pickup_only_items(item_ids):
    """Оголошення «лише самовивіз» (без доставки) серед item_ids — для позначки 🚗 у картках."""
    ids = [i for i in item_ids if i]
    if not ids:
        return set()
    with get_conn() as conn:
        return {r["item_id"] for r in conn.execute(
            f"SELECT DISTINCT item_id FROM listing_obs WHERE pickup_only = 1 AND item_id IN "
            f"({','.join('?' * len(ids))})", ids).fetchall()}


def _activity(table, key, since, keys=None):
    """{ключ: {'total', 'new', 'sold', 'sold_new'}}: усі оголошення, які бот бачив (зберігаються
    LISTING_OBS_RETENTION_DAYS днів), нові з since; продані (як у статистиці продажів) — усього і з since."""
    sold = "(status = 'gone'" + _sold_filter() + ")"
    sql = (f"SELECT {key} AS k, COUNT(*) AS total, SUM(first_seen >= :since) AS new, "
           f"SUM({sold}) AS sold, SUM({sold} AND gone_at >= :since) AS sold_new FROM {table}")
    params: dict = {"since": since}
    if keys is not None:
        if not keys:
            return {}
        names = {f"k{i}": k for i, k in enumerate(keys)}
        sql += f" WHERE {key} IN (" + ",".join(":" + n for n in names) + ")"
        params.update(names)
    sql += f" GROUP BY {key}"
    with get_conn() as conn:
        return {r["k"]: {f: int(r[f] or 0) for f in ("total", "new", "sold", "sold_new")}
                for r in conn.execute(sql, params).fetchall()}


def watch_activity(watch_ids, since):
    """«📊 Статистика»: активність товарів користувача (див. _activity)."""
    return _activity("listing_obs", "watch_id", since, list(watch_ids))


def discovery_activity(since):
    """«📊 Статистика»: активність товарів «💡 Що перепродавати» (див. _activity)."""
    return _activity("discovery_obs", "candidate", since)


# Імпорти з інших частин бази — унизу, щоб модулі могли посилатися один на одного
from core.db.api_usage import _utc_day  # noqa: E402
from core.db.base import get_conn, get_meta, set_meta  # noqa: E402
