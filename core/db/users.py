"""
Користувачі й доступ.
"""

import time


# ---------- users / доступ ----------

def get_user_row(user_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def upsert_user_request(user_id, chat_id, username, first_name):
    with get_conn() as conn:
        existing = conn.execute("SELECT status FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if existing and existing["status"] == "approved":
            return  # вже схвалений, повторний запит не потрібен
        conn.execute(
            """INSERT INTO users (user_id, chat_id, username, first_name, status, requested_at)
               VALUES (?, ?, ?, ?, 'pending', ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 chat_id=excluded.chat_id, username=excluded.username,
                 first_name=excluded.first_name, status='pending',
                 requested_at=excluded.requested_at, decided_at=NULL""",
            (user_id, chat_id, username, first_name, int(time.time())),
        )


def set_user_status(user_id, status):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET status = ?, decided_at = ? WHERE user_id = ?",
            (status, int(time.time()), user_id),
        )


def delete_user(user_id):
    """Повністю прибирає користувача: запис доступу видаляється (він зможе надіслати
    новий запит), його товари перестають відстежуватись (не витрачають ліміт eBay)."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            watch_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM watches WHERE chat_id = ? AND active = 1", (row["chat_id"],)).fetchall()]
            # Як і видалення товару: історія ще зберігається для «↩️ Скасувати»
            conn.execute("UPDATE watches SET active = 0, deleted_at = ? WHERE chat_id = ? AND active = 1",
                         (int(time.time()), row["chat_id"]))
        else:
            watch_ids = []
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        return {**dict(row), "watch_ids": watch_ids} if row else None


def restore_user(user_row, watch_ids):
    """Скасувати видалення користувача: доступ і його товари — як були."""
    cols = ["user_id", "chat_id", "username", "first_name", "status", "requested_at", "decided_at"]
    with get_conn() as conn:
        conn.execute(f"INSERT OR REPLACE INTO users ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [user_row.get(c) for c in cols])
    return sum(restore_watch(wid, user_row["chat_id"]) for wid in watch_ids)


def list_users(status=None):
    q = "SELECT * FROM users"
    params = []
    if status:
        q += " WHERE status = ?"
        params.append(status)
    q += " ORDER BY requested_at DESC"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


# Імпорти з інших частин бази — унизу, щоб модулі могли посилатися один на одного
from core.db.base import get_conn  # noqa: E402
from core.db.watches import restore_watch  # noqa: E402
