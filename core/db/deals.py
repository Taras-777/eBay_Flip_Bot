"""
«🔥 Вигідні пропозиції»: збереження, список, перевірка.
"""

import time

from core import settings


# ---------- deals ----------

def add_deal(watch_id, item_id, title, total_price, currency, median_price, discount_pct, url, suspicious,
             has_best_offer=False, cond_group=None, spec_group=None, listed_at=None,
             item_spec=None, sale_source=None, sale_sample=None, auction=None):
    """auction — {'current_bid', 'bid_count', 'end_at'} для «аукціон + купити зараз».
    Якщо це оголошення вже є у «🔥» без рішення (продавець знизив ціну) — оновлюється той самий запис
    (знову 🆕), а не додається ще один: інакше одне оголошення висіло б у списку п'ять разів."""
    now = int(time.time())
    with get_conn() as conn:
        old = conn.execute("SELECT id FROM deals WHERE watch_id = ? AND item_id = ? AND status = 'new' "
                           "ORDER BY id DESC LIMIT 1", (watch_id, item_id)).fetchone()
        if old:
            conn.execute(
                """UPDATE deals SET title = ?, total_price = ?, median_price = ?, discount_pct = ?, url = ?,
                   suspicious = ?, has_best_offer = ?, created_at = ?, seen_at = NULL, cond_group = ?,
                   spec_group = ?, checked_at = ?, item_spec = ?, sale_source = ?, sale_sample = ? WHERE id = ?""",
                (title, total_price, median_price, discount_pct, url, int(suspicious), int(has_best_offer), now,
                 cond_group, spec_group, now, item_spec, sale_source, sale_sample, old["id"]))
            return old["id"]
        cur = conn.execute(
            """INSERT INTO deals
               (watch_id, item_id, title, total_price, currency, median_price, discount_pct, url, suspicious,
                has_best_offer, status, created_at, cond_group, spec_group, listed_at, checked_at,
                item_spec, sale_source, sale_sample, auction, current_bid, bid_count, end_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (watch_id, item_id, title, total_price, currency, median_price, discount_pct, url,
             int(suspicious), int(has_best_offer), now, cond_group, spec_group, listed_at, now,
             item_spec, sale_source, sale_sample, int(auction is not None),
             (auction or {}).get("current_bid"), (auction or {}).get("bid_count"), (auction or {}).get("end_at")),
        )
        return cur.lastrowid


def get_open_deals(chat_id=None):
    """Усі пропозиції «🔥» без рішення (незалежно від мін. прибутку) — для переоцінки.
    chat_id=None — усіх користувачів. Поле chat_id — власник товару."""
    since = int(time.time()) - DEALS_INBOX_DAYS * 86400
    sql = ("SELECT d.*, w.chat_id AS chat_id FROM deals d JOIN watches w ON w.id = d.watch_id "
           "WHERE w.active = 1 AND d.status = 'new' AND d.created_at >= ?")
    params = [since]
    if chat_id is not None:
        sql += " AND w.chat_id = ?"
        params.append(chat_id)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def update_deal_price(deal_id, sale_price, discount_pct, cond_group, spec_group, item_spec,
                      sale_source, sale_sample):
    """Переоцінка пропозиції за свіжою статистикою ринку."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE deals SET median_price = ?, discount_pct = ?, cond_group = ?, spec_group = ?,
               item_spec = ?, sale_source = ?, sale_sample = ? WHERE id = ?""",
            (sale_price, discount_pct, cond_group, spec_group, item_spec, sale_source, sale_sample, deal_id))


def get_listing_groups(watch_id, item_ids):
    """item_id → (стан, конфігурація) з історії оголошень товару."""
    ids = [i for i in item_ids if i]
    result = {}
    with get_conn() as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for r in conn.execute(
                    "SELECT item_id, cond_group, spec_group FROM listing_obs WHERE watch_id = ? AND item_id IN ("
                    + ",".join("?" * len(chunk)) + ")", [watch_id, *chunk]):
                if r["cond_group"]:
                    result[r["item_id"]] = (r["cond_group"], r["spec_group"] or "unspecified")
    return result


def set_deal_status(deal_id, status):
    with get_conn() as conn:
        conn.execute("UPDATE deals SET status = ? WHERE id = ?", (status, deal_id))


def get_deal(deal_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        return dict(row) if row else None


def get_deal_owner_chat_id(deal_id):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT w.chat_id FROM deals d
               JOIN watches w ON w.id = d.watch_id WHERE d.id = ?""",
            (deal_id,),
        ).fetchone()
        return row["chat_id"] if row else None


DEALS_INBOX_DAYS = 7   # скільки днів пропозиція висить у «🔥 Вигідні пропозиції»


def _profit_sql():
    """Орієнтовний прибуток пропозиції (як estimate_resale_profit) — для фільтра в SQL."""
    return (f"(d.median_price * (1 - {float(settings.EBAY_SELLING_FEES_PCT)} / 100.0) "
            f"- {float(settings.RESALE_SHIPPING_EUR)} - d.total_price)")


def get_inbox_deals(chat_id, limit=5, offset=0):
    """Актуальні вигідні пропозиції користувача (ще без рішення, з прибутком не нижче
    його мінімального), від найбільшого прибутку. Повертає (сторінка, загальна кількість)."""
    since = int(time.time()) - DEALS_INBOX_DAYS * 86400
    params = (chat_id, since)
    with get_conn() as conn:
        where = ("w.chat_id = ? AND w.active = 1 AND d.status = 'new' AND d.created_at >= ? AND "
                 + _profit_sql() + " >= " + _min_profit_sql(conn, chat_id) + " AND " + SOLD_ONLY_SQL)
        rows = conn.execute(
            "SELECT d.*, w.label AS watch_label FROM deals d JOIN watches w ON w.id = d.watch_id WHERE "
            + where + " ORDER BY " + _profit_sql() + " DESC, d.created_at DESC LIMIT ? OFFSET ?",
            params + (limit, offset),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM deals d JOIN watches w ON w.id = d.watch_id WHERE " + where, params,
        ).fetchone()["c"]
    return [dict(r) for r in rows], total


def count_unseen_deals(chat_id):
    since = int(time.time()) - DEALS_INBOX_DAYS * 86400
    with get_conn() as conn:
        return conn.execute(
            """SELECT COUNT(*) AS c FROM deals d JOIN watches w ON w.id = d.watch_id
               WHERE w.chat_id = ? AND w.active = 1 AND d.status = 'new' AND d.seen_at IS NULL
               AND d.created_at >= ? AND """ + _profit_sql() + " >= " + _min_profit_sql(conn, chat_id)
            + " AND " + SOLD_ONLY_SQL,
            (chat_id, since),
        ).fetchone()["c"]


def get_deals_to_recheck(limit, stale_seconds, deal_ids=None):
    """Пропозиції зі «🔥 Вигідні пропозиції», які давно не перевіряли, чи лот ще продається
    (лише ті, що проходять мін. прибуток свого користувача). Найдавніше перевірені — першими.
    deal_ids — обмежитись цими (сторінка, яку користувач зараз відкриває)."""
    now = int(time.time())
    if deal_ids is not None and not deal_ids:
        return []
    with get_conn() as conn:
        sql = ("SELECT d.id, d.item_id, d.watch_id, d.total_price, d.median_price, d.auction "
               "FROM deals d JOIN watches w ON w.id = d.watch_id "
               "WHERE w.active = 1 AND d.status = 'new' AND d.created_at >= ? "
               "AND COALESCE(d.checked_at, d.created_at) < ? AND " + _profit_sql() + " >= " + _min_profit_sql(conn)
               + " AND " + SOLD_ONLY_SQL)
        params = [now - DEALS_INBOX_DAYS * 86400, now - stale_seconds]
        if deal_ids is not None:
            sql += " AND d.id IN (" + ",".join("?" * len(deal_ids)) + ")"
            params += list(deal_ids)
        sql += " ORDER BY COALESCE(d.checked_at, d.created_at) LIMIT ?"
        return [dict(r) for r in conn.execute(sql, params + [limit]).fetchall()]


def set_deal_total_price(deal_id, total_price):
    """Продавець змінив ціну (з доставкою) — пропозиція рахується вже за новою ціною. Якщо вона
    перестала бути вигідною, у «🔥» більше не показується (фільтр мін. прибутку в запитах)."""
    with get_conn() as conn:
        conn.execute("UPDATE deals SET total_price = ? WHERE id = ?", (round(float(total_price), 2), deal_id))


def mark_deal_checked(deal_id, gone=False, watch_count=None):
    """Результат перевірки: gone=True — лот продано/знято, пропозиція зникає зі списку.
    watch_count — скільки людей стежать (None — eBay не сказав, лишаємо попереднє)."""
    with get_conn() as conn:
        if gone:
            conn.execute("UPDATE deals SET status = 'gone', checked_at = ? WHERE id = ? AND status = 'new'",
                         (int(time.time()), deal_id))
        else:
            conn.execute("UPDATE deals SET checked_at = ?, watch_count = COALESCE(?, watch_count) WHERE id = ?",
                         (int(time.time()), watch_count, deal_id))


def mark_deals_seen(deal_ids):
    if not deal_ids:
        return
    with get_conn() as conn:
        conn.executemany("UPDATE deals SET seen_at = ? WHERE id = ? AND seen_at IS NULL",
                         [(int(time.time()), i) for i in deal_ids])


def clear_inbox_deals(chat_id):
    """«🧹 Очистити список» — усі актуальні пропозиції позначаються як пропущені."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE deals SET status = 'skipped', seen_at = COALESCE(seen_at, ?)
               WHERE status = 'new' AND watch_id IN (SELECT id FROM watches WHERE chat_id = ?)""",
            (int(time.time()), chat_id),
        )


def get_deal_stats(chat_id):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT d.status, COUNT(*) AS c FROM deals d
               JOIN watches w ON w.id = d.watch_id
               WHERE w.chat_id = ?
               GROUP BY d.status""",
            (chat_id,),
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}


# Імпорти з інших частин бази — унизу, щоб модулі могли посилатися один на одного
from core.db.base import get_conn  # noqa: E402
from core.db.markdowns import SOLD_ONLY_SQL  # noqa: E402
from core.db.profit import _min_profit_sql  # noqa: E402
