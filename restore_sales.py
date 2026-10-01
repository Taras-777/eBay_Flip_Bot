"""
Повертає продажі товару (і зниклі без продажу та історію цін) з резервної копії бази.
Потрібно, якщо історію стерло, як раніше при зміні категорії. Поточні дані не чіпає:
додає лише записи, яких у базі зараз немає.

Запуск на сервері (з папки ~/eBay_Flip_Bot):
    docker compose exec bot python restore_sales.py "15 pro"
"""

import gzip
import os
import shutil
import sqlite3
import sys
import tempfile

import settings
from backup import list_backups


def _connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def _find_watch(live, needle):
    rows = live.execute("SELECT id, label, query FROM watches WHERE deleted_at IS NULL").fetchall()
    return [r for r in rows if needle in (r["label"] or "").lower() or needle in (r["query"] or "").lower()]


def _copy(live, old, table, where, params):
    """INSERT OR IGNORE рядків з копії; повертає кількість доданих."""
    cols = [c for c in _columns(old, table) if c in set(_columns(live, table))]
    names = ", ".join(cols)
    rows = old.execute(f"SELECT {names} FROM {table} WHERE {where}", params).fetchall()
    before = live.total_changes
    live.executemany(f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({', '.join('?' * len(cols))})",
                     [tuple(r[c] for c in cols) for r in rows])
    return live.total_changes - before


def main():
    if len(sys.argv) < 2:
        print('Вкажи частину назви товару: python restore_sales.py "15 pro"')
        return 1
    live = _connect(settings.DB_PATH)
    found = _find_watch(live, sys.argv[1].strip().lower())
    if len(found) != 1:
        print("Знайдено товарів: %s. Уточни назву." % len(found))
        for r in found:
            print(f"  #{r['id']} {r['label']}")
        return 1
    watch = found[0]
    wid = watch["id"]

    # Копія з найбільшою кількістю продажів цього товару: копія, зроблена вже ПІСЛЯ того,
    # як історію стерло, теж містить кілька свіжих продажів, але не стару історію
    best, best_count = None, 0
    with tempfile.TemporaryDirectory() as tmp:
        for n, path in enumerate(list_backups()):
            raw = os.path.join(tmp, f"backup{n}.sqlite3")
            with gzip.open(path, "rb") as f_in, open(raw, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            old = _connect(raw)
            try:
                count = old.execute("SELECT COUNT(*) AS c FROM listing_obs WHERE watch_id = ? AND status != 'active'",
                                    (wid,)).fetchone()["c"]
            finally:
                old.close()
            if count > best_count:
                if best:
                    os.remove(best[1])   # розпаковані копії великі — тримаємо лише найкращу
                best, best_count = (path, raw), count
            else:
                os.remove(raw)
        if best is None:
            print(f"У резервних копіях немає продажів для «{watch['label']}».")
            return 1
        old = _connect(best[1])
        try:
            with live:
                added = _copy(live, old, "listing_obs", "watch_id = ? AND status != 'active'", (wid,))
                history = _copy(live, old, "price_history", "watch_id = ?", (wid,))
                live.execute("UPDATE market_stats SET updated_at = 0 WHERE watch_id = ?", (wid,))
        finally:
            old.close()
    print(f"«{watch['label']}»: з копії {os.path.basename(best[0])} повернуто продажів і зниклих: {added} "
          f"(у копії було {best_count}), днів історії цін: {history}.")
    print("Ціни перерахуються найближчим циклом (до 5 хв).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
