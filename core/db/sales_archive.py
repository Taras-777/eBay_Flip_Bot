"""
«🗃 Архів продажів»: продажі, старші за LISTING_OBS_RETENTION_DAYS днів, не видаляються, а переносяться
в компактну таблицю sales_archive і зберігаються назавжди — для «💶 Оцінити товар».

Що зберігається: лише продані (✅ підтверджені eBay і «зникли» без відповіді eBay) — товар (пошуковий
запит), назва, клас / конфігурація, тип продавця (cond_group: used — 👤 приватний), ціна, коли виставлено
й коли продано. Непродані оголошення не зберігаються. Жодних даних продавців.
"""


def _archive_where(alias=""):
    p = f"{alias}." if alias else ""
    return (f"{p}status = 'gone' AND {p}price IS NOT NULL AND {p}gone_at IS NOT NULL "
            f"AND COALESCE({p}sold_check, '') NOT IN ('unsold', 'active')")


def archive_watch_sales(conn, where_sql, params=()):
    """Перенести в архів продажі товарів з listing_obs, що підпадають під where_sql (поля listing_obs, аліас o).
    Повертає кількість нових рядків архіву. Видаляти рядки listing_obs — справа того, хто викликає."""
    return conn.execute(
        f"""INSERT OR IGNORE INTO sales_archive
              (query, item_id, source, title, spec_group, cond_group, price, listed_at, sold_at, confirmed)
            SELECT w.query, o.item_id, 'watch', o.title, o.spec_group, o.cond_group, o.price,
                   COALESCE(o.created_at, o.first_seen), o.gone_at, o.sold_check = 'sold'
            FROM listing_obs o JOIN watches w ON w.id = o.watch_id
            WHERE {_archive_where('o')} AND ({where_sql})""", params).rowcount


def archive_discovery_sales(queries, cutoff, prefix=""):
    """Перенести в архів продажі «💡» (і оцінених товарів), старші за cutoff, і видалити всі рядки
    discovery_obs, старші за cutoff. queries: {назва кандидата: пошуковий запит}; для невідомих назв
    (кандидата прибрали зі списку) запитом стає сама назва без prefix. Повертає (в архів, видалено)."""
    archived = 0
    with get_conn() as conn:
        names = [r["candidate"] for r in conn.execute(
            "SELECT DISTINCT candidate FROM discovery_obs WHERE last_seen < ?", (cutoff,)).fetchall()]
        for name in names:
            query = queries.get(name) or (name[len(prefix):] if prefix and name.startswith(prefix) else name)
            archived += conn.execute(
                f"""INSERT OR IGNORE INTO sales_archive
                      (query, item_id, source, title, spec_group, cond_group, price, listed_at, sold_at, confirmed)
                    SELECT ?, item_id, ?, NULL, spec_group, cond_group, price,
                           COALESCE(created_at, first_seen), gone_at, sold_check = 'sold'
                    FROM discovery_obs WHERE candidate = ? AND last_seen < ? AND {_archive_where()}""",
                (query, "appr" if prefix and name.startswith(prefix) else "disc", name, cutoff)).rowcount
        deleted = conn.execute("DELETE FROM discovery_obs WHERE last_seen < ?", (cutoff,)).rowcount
    return archived, deleted


def archived_queries():
    """Усі пошукові запити, продажі яких є в архіві (для пошуку при оцінці)."""
    with get_conn() as conn:
        return [r["query"] for r in conn.execute("SELECT DISTINCT query FROM sales_archive").fetchall()]


def get_archived_sales(queries, since=None, until=None):
    """Продажі з архіву для запитів queries, найновіші першими. З «лише підтверджені» — лише ✅."""
    queries = list(queries)
    if not queries:
        return []
    sql = ("SELECT item_id, query, title, spec_group, cond_group, price, listed_at AS created_at, "
           "sold_at AS gone_at, confirmed FROM sales_archive WHERE query IN (" + ",".join("?" * len(queries)) + ")")
    params = list(queries)
    if since is not None:
        sql += " AND sold_at >= ?"
        params.append(int(since))
    if until is not None:
        sql += " AND sold_at < ?"
        params.append(int(until))
    if get_meta("ebay_user_refresh_token") and get_confirmed_only():
        sql += " AND confirmed = 1"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY sold_at DESC", params).fetchall()]


def sales_archive_stats():
    """(скільки продажів в архіві, найстаріший продаж — timestamp або None)."""
    with get_conn() as conn:
        r = conn.execute("SELECT COUNT(*) AS c, MIN(sold_at) AS m FROM sales_archive").fetchone()
    return int(r["c"] or 0), r["m"]


# Імпорти з інших частин бази — унизу, щоб модулі могли посилатися один на одного
from core.db.base import get_conn, get_meta  # noqa: E402
from core.db.observations import get_confirmed_only  # noqa: E402
