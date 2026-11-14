"""
«🔨 Аукціони».
"""

import time


# ---------- «🔨 Аукціони» ----------

def get_auctions_enabled(chat_id):
    """Пошук аукціонів увімкнено в «⚙️ Налаштування» (за замовчуванням вимкнено — витрачає ліміт eBay)."""
    return get_meta(f"auctions:{chat_id}") == "1"


def set_auctions_enabled(chat_id, value):
    set_meta(f"auctions:{chat_id}", "1" if value else "0")


def auction_chats():
    """Чати, де пошук аукціонів увімкнено."""
    with get_conn() as conn:
        rows = conn.execute("SELECT key FROM meta WHERE key LIKE 'auctions:%' AND value = '1'").fetchall()
    return {int(r["key"].split(":", 1)[1]) for r in rows}


def get_alerted_auctions(chat_id, item_ids):
    """Аукціони, про які в цьому чаті вже сповіщали."""
    ids = list(item_ids)
    if not ids:
        return set()
    found = set()
    with get_conn() as conn:
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            found |= {r["item_id"] for r in conn.execute(
                f"SELECT item_id FROM auction_alerts WHERE chat_id = ? AND item_id IN ({','.join('?' * len(part))})",
                [chat_id, *part]).fetchall()}
    return found


def add_auction_alert(chat_id, item_id, watch_id, end_at, buy_limit, shipping, bid, text, url, title=None,
                      bid_count=None):
    """Запам'ятати вигідний аукціон. False — про нього в цьому чаті вже сповіщали."""
    now = int(time.time())
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO auction_alerts
               (chat_id, item_id, watch_id, end_at, buy_limit, shipping, bid, text, url, at, title, checked_at,
                bid_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, item_id, watch_id, int(end_at), buy_limit, shipping or 0, bid, text, url, now, title, now,
             bid_count))
        return cur.rowcount > 0


def list_auctions(chat_id, now=None):
    """«🔨 Аукціони»: знайдені вигідні аукціони, що ще тривають (без прихованих), — найближчі до кінця першими."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT a.*, w.label AS watch_label FROM auction_alerts a JOIN watches w ON w.id = a.watch_id
               WHERE a.chat_id = ? AND a.end_at > ? AND COALESCE(a.hidden, 0) = 0 AND w.active = 1
               ORDER BY a.end_at""", (chat_id, int(now or time.time()))).fetchall()]


def list_auctions_all(now=None):
    """Аукціони всіх користувачів, що ще тривають і не сховані."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM auction_alerts WHERE end_at > ? AND COALESCE(hidden, 0) = 0",
            (int(now or time.time()),)).fetchall()]


def count_auctions(chat_id):
    with get_conn() as conn:
        return conn.execute(
            """SELECT COUNT(*) AS c FROM auction_alerts a JOIN watches w ON w.id = a.watch_id
               WHERE a.chat_id = ? AND a.end_at > ? AND COALESCE(a.hidden, 0) = 0 AND w.active = 1""",
            (chat_id, int(time.time()))).fetchone()["c"]


def hide_auction(chat_id, item_id):
    """🙈 — прибрати з «🔨 Аукціони» і більше не нагадувати."""
    with get_conn() as conn:
        conn.execute("UPDATE auction_alerts SET hidden = 1, final_at = COALESCE(final_at, ?) "
                     "WHERE chat_id = ? AND item_id = ?", (int(time.time()), chat_id, item_id))


def update_auction_bid(chat_id, item_id, bid, bid_count):
    with get_conn() as conn:
        conn.execute("UPDATE auction_alerts SET bid = COALESCE(?, bid), bid_count = COALESCE(?, bid_count), "
                     "checked_at = ? WHERE chat_id = ? AND item_id = ?",
                     (bid, bid_count, int(time.time()), chat_id, item_id))


def set_auction_message(chat_id, item_id, message_id):
    with get_conn() as conn:
        conn.execute("UPDATE auction_alerts SET message_id = ? WHERE chat_id = ? AND item_id = ?",
                     (message_id, chat_id, item_id))


def due_auction_finals(minutes, now=None):
    """Аукціони, яким до кінця лишилось ≤ minutes, а друге сповіщення ще не надсилали."""
    now = int(now or time.time())
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM auction_alerts WHERE final_at IS NULL AND end_at > ? AND end_at <= ?
               ORDER BY end_at""", (now, now + minutes * 60)).fetchall()
    return [dict(r) for r in rows]


def mark_auction_final(chat_id, item_id, bid=None):
    with get_conn() as conn:
        conn.execute("UPDATE auction_alerts SET final_at = ?, bid = COALESCE(?, bid) WHERE chat_id = ? AND item_id = ?",
                     (int(time.time()), bid, chat_id, item_id))


def cleanup_auction_alerts(days=3):
    """Давно завершені аукціони більше не потрібні."""
    with get_conn() as conn:
        return conn.execute("DELETE FROM auction_alerts WHERE end_at < ?",
                            (int(time.time() - days * 86400),)).rowcount


# Імпорти з інших частин бази — унизу, щоб модулі могли посилатися один на одного
from core.db.base import get_conn, get_meta, set_meta  # noqa: E402
