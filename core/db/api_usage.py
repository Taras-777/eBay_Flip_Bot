"""
Лічильник запитів до eBay.
"""

import sqlite3
import time
from datetime import datetime, timezone

from core.settings import (
    log,
)


# ---------- лічильник запитів до eBay ----------

def _utc_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def record_api_call(api):
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO api_usage (day, api, count) VALUES (?, ?, 1)
                   ON CONFLICT(day, api) DO UPDATE SET count = count + 1""",
                (_utc_day(), api),
            )
    except sqlite3.Error as e:
        log.debug("Не вдалося записати лічильник запитів: %s", e)


def get_api_calls_today(api="browse"):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count FROM api_usage WHERE day = ? AND api = ?", (_utc_day(), api)
        ).fetchone()
        return row["count"] if row else 0


def api_usage_by_day(days=60):
    """{день 'YYYY-MM-DD' (UTC): {api: кількість}} за останні days днів, найновіші першими."""
    oldest = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).strftime("%Y-%m-%d")
    result: dict = {}
    with get_conn() as conn:
        for r in conn.execute("SELECT day, api, count FROM api_usage WHERE day > ? ORDER BY day DESC",
                              (oldest,)).fetchall():
            result.setdefault(r["day"], {})[r["api"]] = r["count"]
    return result


# Імпорти з інших частин бази — унизу, щоб модулі могли посилатися один на одного
from core.db.base import get_conn  # noqa: E402
