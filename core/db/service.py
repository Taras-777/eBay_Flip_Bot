"""
Журнал повільних дій і «🛠 Нерозпізнані».
"""

import time


# ---------- 🐢 Журнал повільних дій ----------

ACTION_LOG_KEEP = 1000


def record_action(chat_id, action, seconds, error=None):
    """Повільна (або з помилкою) дія користувача — у журнал; зберігаються останні ACTION_LOG_KEEP."""
    with get_conn() as conn:
        conn.execute("INSERT INTO action_log (at, chat_id, action, seconds, error) VALUES (?, ?, ?, ?, ?)",
                     (int(time.time()), chat_id, action, round(seconds, 2), error))
        conn.execute("DELETE FROM action_log WHERE id <= (SELECT MAX(id) FROM action_log) - ?", (ACTION_LOG_KEEP,))


def get_action_log(limit=15):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def action_log_summary(days=7, limit=10):
    """Найповільніші дії за `days` днів: [{'action', 'count', 'avg', 'max', 'errors'}], від найгіршої."""
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT action, COUNT(*) AS count, AVG(seconds) AS avg, MAX(seconds) AS max,
                      SUM(error IS NOT NULL) AS errors
               FROM action_log WHERE at >= ? GROUP BY action ORDER BY MAX(seconds) * COUNT(*) DESC LIMIT ?""",
            (since, limit)).fetchall()]


def clear_action_log():
    with get_conn() as conn:
        conn.execute("DELETE FROM action_log")


# ---------- 🛠 Нерозпізнані оголошення ----------

def save_manual_spec(item_id, spec_group):
    """Клас/конфігурацію оголошення вказав користувач — бот її не перезаписує."""
    with get_conn() as conn:
        conn.execute("""INSERT INTO manual_specs (item_id, spec_group, at) VALUES (?, ?, ?)
                        ON CONFLICT(item_id) DO UPDATE SET spec_group = excluded.spec_group, at = excluded.at""",
                     (item_id, spec_group, int(time.time())))


def get_manual_specs(item_ids=None):
    """{item_id: конфігурація, вказана вручну}; item_ids=None — усі."""
    with get_conn() as conn:
        if item_ids is None:
            rows = conn.execute("SELECT item_id, spec_group FROM manual_specs").fetchall()
        else:
            ids = list(item_ids)
            rows = []
            for start in range(0, len(ids), 500):
                part = ids[start:start + 500]
                rows += conn.execute(f"SELECT item_id, spec_group FROM manual_specs WHERE item_id IN "
                                     f"({','.join('?' * len(part))})", part).fetchall()
    return {r["item_id"]: r["spec_group"] for r in rows}


def set_review(watch_id, item_id, status, title=None, price=None, url=None, note=None):
    """status: 'keep' — «👌 Залишити як є», 'flag' — «🚩 Для розробника»."""
    with get_conn() as conn:
        conn.execute("""INSERT INTO review_items (watch_id, item_id, status, title, price, url, note, at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(watch_id, item_id) DO UPDATE SET status = excluded.status,
                          title = excluded.title, price = excluded.price, url = excluded.url,
                          note = excluded.note, at = excluded.at""",
                     (watch_id, item_id, status, title, price, url, note, int(time.time())))


def get_reviewed_ids(watch_id):
    with get_conn() as conn:
        return {r["item_id"] for r in conn.execute("SELECT item_id FROM review_items WHERE watch_id = ?",
                                                   (watch_id,)).fetchall()}


def get_flagged(watch_id):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM review_items WHERE watch_id = ? AND status = 'flag' ORDER BY at", (watch_id,)).fetchall()]


def clear_flagged(watch_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM review_items WHERE watch_id = ? AND status = 'flag'", (watch_id,))


def forget_seen_item(watch_id, item_id):
    """Оголошення оціниться заново наступного циклу (напр. після того, як користувач вказав клас)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM seen_items WHERE watch_id = ? AND item_id = ?", (watch_id, item_id))


# Імпорти з інших частин бази — унизу, щоб модулі могли посилатися один на одного
from core.db.base import get_conn  # noqa: E402
