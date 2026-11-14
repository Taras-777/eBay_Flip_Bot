"""
«❌ Не той товар», вивчені слова і «↩️ Скасувати».
"""

import json
import time


# ---------- «🚫 Не той товар» і вивчені слова ----------

def reject_item(watch_id, item_id, title, reason="wrong"):
    """Позначає лот як чужий для цього товару: більше не потрапляє ні в
    ринкову статистику, ні в знахідки. Прибираємо його і з поточних
    спостережень, щоб ціна не зарахувалась як «зниклий» (проданий) лот."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO rejected_items (watch_id, item_id, title, created_at, reason) VALUES (?, ?, ?, ?, ?)",
            (watch_id, item_id, title or "", int(time.time()), reason),
        )
        conn.execute("DELETE FROM listing_obs WHERE watch_id = ? AND item_id = ?", (watch_id, item_id))


def get_rejected_ids(watch_id):
    with get_conn() as conn:
        return {r["item_id"] for r in conn.execute(
            "SELECT item_id FROM rejected_items WHERE watch_id = ?", (watch_id,)).fetchall()}


def get_rejected_titles(watch_id):
    with get_conn() as conn:
        return [r["title"] for r in conn.execute(
            "SELECT title FROM rejected_items WHERE watch_id = ? AND title != '' AND reason = 'wrong'",
            (watch_id,)).fetchall()]


def get_learned_words(watch_id):
    """{слово: статус} для товару."""
    with get_conn() as conn:
        return {r["word"]: r["status"] for r in conn.execute(
            "SELECT word, status FROM learned_words WHERE watch_id = ?", (watch_id,)).fetchall()}


def set_learned_word(watch_id, word, status):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO learned_words (watch_id, word, status, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(watch_id, word) DO UPDATE SET status = excluded.status""",
            (watch_id, word, status, int(time.time())),
        )


def delete_listing_obs_by_ids(watch_id, item_ids):
    if not item_ids:
        return
    with get_conn() as conn:
        conn.executemany(
            "DELETE FROM listing_obs WHERE watch_id = ? AND item_id = ?",
            [(watch_id, i) for i in item_ids],
        )


# ---------- «↩️ Скасувати» ----------

def get_obs_rows(watch_id, item_ids):
    """Знімок спостережень перед видаленням — щоб скасування повернуло їх як були."""
    item_ids = list(item_ids)
    if not item_ids:
        return []
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM listing_obs WHERE watch_id = ? AND item_id IN ({','.join('?' * len(item_ids))})",
            [watch_id] + item_ids).fetchall()]


def restore_obs_rows(rows):
    with get_conn() as conn:
        for r in rows:
            cols = list(r)
            conn.execute(f"INSERT OR IGNORE INTO listing_obs ({', '.join(cols)}) "
                         f"VALUES ({', '.join('?' * len(cols))})", [r[c] for c in cols])


def unreject_item(watch_id, item_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM rejected_items WHERE watch_id = ? AND item_id = ?", (watch_id, item_id))


def forget_learned_words(watch_id, words):
    """Слово знову «не вивчене» (а не «скасоване») — бот зможе запропонувати його пізніше."""
    with get_conn() as conn:
        conn.executemany("DELETE FROM learned_words WHERE watch_id = ? AND word = ?",
                         [(watch_id, w) for w in words])


def set_deals_status(deal_ids, status):
    with get_conn() as conn:
        conn.executemany("UPDATE deals SET status = ? WHERE id = ?", [(status, i) for i in deal_ids])


def inbox_deal_ids(chat_id):
    with get_conn() as conn:
        return [r["id"] for r in conn.execute(
            """SELECT d.id FROM deals d JOIN watches w ON w.id = d.watch_id
               WHERE w.chat_id = ? AND d.status = 'new'""", (chat_id,)).fetchall()]


def add_undo(chat_id, kind, label, payload):
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO undo_actions (chat_id, kind, label, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, kind, label, json.dumps(payload, ensure_ascii=False), int(time.time())),
        ).lastrowid


def get_undo(undo_id, chat_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM undo_actions WHERE id = ? AND chat_id = ?", (undo_id, chat_id)).fetchone()
    if not row:
        return None
    return {**dict(row), "payload": json.loads(row["payload"])}


def latest_undo(chat_id, since):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, label FROM undo_actions WHERE chat_id = ? AND done = 0 AND created_at >= ? "
            "ORDER BY id DESC LIMIT 1", (chat_id, since)).fetchone()
    return dict(row) if row else None


def mark_undo_done(undo_id):
    """True — позначили зараз; False — вже скасовано раніше (подвійне натискання)."""
    with get_conn() as conn:
        return bool(conn.execute("UPDATE undo_actions SET done = 1 WHERE id = ? AND done = 0",
                                 (undo_id,)).rowcount)


# Імпорти з інших частин бази — унизу, щоб модулі могли посилатися один на одного
from core.db.base import get_conn  # noqa: E402
