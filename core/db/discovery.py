"""
«💡 Що перепродавати»: спостереження й результати аналізу.
"""

import json
import time

from core.settings import (
    SOLD_CHECK_MAX_AGE_DAYS,
)


# ---------- «💡 Що перепродавати» ----------

def update_discovery_obs(candidate, items, window_start):
    """
    Зберігає поточні оголошення кандидата і позначає «зниклими» ті, що мали б
    бути у видачі (створені після найстарішого оголошення поточної видачі),
    але їх немає — найімовірніше, їх купили. Повертає кількість нових зниклих.
    """
    now = int(time.time())
    present = {it["item_id"] for it in items}
    with get_conn() as conn:
        for it in items:
            conn.execute(
                """INSERT INTO discovery_obs (candidate, item_id, price, spec_group, cond_group,
                                              created_at, first_seen, last_seen, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
                   ON CONFLICT(candidate, item_id) DO UPDATE SET
                     price = excluded.price, last_seen = excluded.last_seen, status = 'active',
                     gone_at = NULL, sold_check = NULL""",
                (candidate, it["item_id"], it["total_price"], it.get("spec_group"), it.get("cond_group"),
                 it.get("created_at"), now, now),
            )
        gone = 0
        if window_start:
            rows = conn.execute(
                """SELECT item_id FROM discovery_obs WHERE candidate = ? AND status = 'active'
                   AND created_at >= ?""", (candidate, window_start)).fetchall()
            for r in rows:
                if r["item_id"] not in present:
                    conn.execute("UPDATE discovery_obs SET status = 'gone', gone_at = ?, sold_check = 'pending' "
                                 "WHERE candidate = ? AND item_id = ?", (now, candidate, r["item_id"]))
                    gone += 1
        conn.execute("DELETE FROM discovery_obs WHERE last_seen < ?", (now - 30 * 86400,))
        return gone


def discovery_gone_stats(candidate, days=7):
    """(скільки оголошень зникло за `days` днів, скільки днів уже спостерігаємо)."""
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        gone = conn.execute(
            "SELECT COUNT(*) AS c FROM discovery_obs WHERE candidate = ? AND status = 'gone' AND gone_at >= ?"
            + _sold_filter(), (candidate, since)).fetchone()["c"]
        first = conn.execute("SELECT MIN(first_seen) AS f FROM discovery_obs WHERE candidate = ?",
                             (candidate,)).fetchone()["f"]
    observed_days = (time.time() - first) / 86400 if first else 0
    return gone, min(observed_days, days)


def get_discovery_sold(candidate, days=14):
    """Продані (зниклі й не спростовані eBay) оголошення кандидата за `days` днів."""
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT item_id, price, spec_group, cond_group, created_at, first_seen, gone_at, sold_check
               FROM discovery_obs WHERE candidate = ? AND status = 'gone' AND gone_at >= ?
               AND price IS NOT NULL""" + _sold_filter() + " ORDER BY gone_at DESC",
            (candidate, since),
        ).fetchall()]


def get_pending_discovery_checks(limit):
    since = int(time.time()) - SOLD_CHECK_MAX_AGE_DAYS * 86400
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT candidate, item_id FROM discovery_obs
               WHERE sold_check = 'pending' AND gone_at >= ? ORDER BY gone_at DESC LIMIT ?""",
            (since, limit),
        ).fetchall()]


def apply_discovery_check(candidate, item_id, result, details=None):
    """Як apply_sold_check, але для «💡 Що перепродавати»: знятий чи ще активний лот
    перестає вважатися проданим."""
    status, mark = SOLD_CHECK_OUTCOMES.get(result, (None, "unknown"))
    with get_conn() as conn:
        conn.execute(
            """UPDATE discovery_obs SET sold_check = ?, checked_at = ?, status = COALESCE(?, status)
               WHERE candidate = ? AND item_id = ? AND sold_check = 'pending'""",
            (mark, int(time.time()), status, candidate, item_id),
        )


def save_discovery_result(candidate, data):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO discovery_results (candidate, data_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(candidate) DO UPDATE SET data_json = excluded.data_json,
                                                    updated_at = excluded.updated_at""",
            (candidate, json.dumps(data, ensure_ascii=False), int(time.time())),
        )


def get_discovery_results():
    with get_conn() as conn:
        rows = conn.execute("SELECT data_json, updated_at FROM discovery_results").fetchall()
    return [dict(json.loads(r["data_json"]), updated_at=r["updated_at"]) for r in rows]


# Імпорти з інших частин бази — унизу, щоб модулі могли посилатися один на одного
from core.db.base import get_conn  # noqa: E402
from core.db.observations import SOLD_CHECK_OUTCOMES, _sold_filter  # noqa: E402
