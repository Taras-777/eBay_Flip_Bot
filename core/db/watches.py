"""
Товари (watches), їх категорії й папки, ринкова статистика товару.
"""

import json
import time
from datetime import datetime, timezone

from core import settings
from core.settings import (
    API_USAGE_RETENTION_DAYS,
    DEALS_RETENTION_DAYS,
    DEFAULT_DISCOUNT_THRESHOLD_PCT,
    SEEN_ITEMS_RETENTION_DAYS,
)


# ---------- watches ----------

def add_watch(chat_id, label, query, exclude, condition_ids, discount_threshold_pct=DEFAULT_DISCOUNT_THRESHOLD_PCT,
              category_id="", category_name="", min_price=0, require_spec=None, required_aspect=None,
              categories=None):
    if categories:
        category_id, category_name = categories[0]["id"], categories[0]["name"]
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO watches
               (chat_id, label, query, exclude, condition_ids, discount_threshold_pct, active, created_at,
                category_id, category_name, min_price, require_spec, required_aspect, categories_json)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, label, query, exclude, condition_ids, discount_threshold_pct, int(time.time()),
             category_id or "", category_name or "", float(min_price or 0), require_spec, required_aspect,
             json.dumps(categories, ensure_ascii=False) if categories else None),
        )
        return cur.lastrowid


def list_watches(chat_id=None, active_only=True):
    q = "SELECT * FROM watches WHERE 1=1"
    params = []
    if chat_id is not None:
        q += " AND chat_id = ?"
        params.append(chat_id)
    if active_only:
        q += " AND active = 1"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def get_watch_by_id(watch_id):
    """Товар за id (будь-якого користувача, лише активний) або None."""
    return next((w for w in list_watches() if w["id"] == watch_id), None)


def get_watch(watch_id, chat_id):
    return next((w for w in list_watches(chat_id=chat_id) if w["id"] == watch_id), None)


def find_duplicate_watch(chat_id, query):
    """Шукає серед активних відстежень цього чату вже існуюче з такою
    ж назвою (без урахування регістру й зайвих пробілів). Повертає
    рядок watch або None."""
    normalized = " ".join(query.strip().lower().split())
    for w in list_watches(chat_id=chat_id):
        if " ".join(w["query"].strip().lower().split()) == normalized:
            return w
    return None


def _purge_watch_data(conn, watch_id):
    for table in ("market_stats", "seen_items", "listing_obs", "price_history", "scan_stats", "price_track"):
        conn.execute(f"DELETE FROM {table} WHERE watch_id = ?", (watch_id,))


def remove_watch(watch_id, chat_id):
    """Товар перестає відстежуватись одразу, а його історія ще UNDO_KEEP_HOURS
    зберігається — щоб «↩️ Скасувати» повернуло все як було."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE watches SET active = 0, deleted_at = ? WHERE id = ? AND chat_id = ? AND active = 1",
            (int(time.time()), watch_id, chat_id),
        )


# ---------- «🗄 Архів»: товар не сканується, але історія й налаштування лишаються ----------

ARCHIVE_FRESH_SECONDS = 3600   # повернули раніше — поточні оголошення ще актуальні, не збираємо заново

def archive_watch(watch_id, chat_id):
    """Перемістити товар в архів. True — переміщено."""
    with get_conn() as conn:
        return bool(conn.execute(
            "UPDATE watches SET active = 0, archived_at = ?, deleted_at = NULL WHERE id = ? AND chat_id = ? AND active = 1",
            (int(time.time()), watch_id, chat_id)).rowcount)


def unarchive_watch(watch_id, chat_id):
    """Повернути товар з архіву. Продажі й налаштування лишаються, а «поточні» оголошення з часу архівації
    збираються заново — інакше ті, що зникли за цей час, бот прийняв би за свіжі продажі. True — повернуто."""
    with get_conn() as conn:
        row = conn.execute("SELECT archived_at FROM watches WHERE id = ? AND chat_id = ? AND archived_at IS NOT NULL",
                           (watch_id, chat_id)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE watches SET active = 1, archived_at = NULL WHERE id = ?", (watch_id,))
        conn.execute("UPDATE market_stats SET updated_at = 0 WHERE watch_id = ?", (watch_id,))
    if time.time() - row["archived_at"] > ARCHIVE_FRESH_SECONDS:
        reset_active_listings(watch_id)   # щойно архівований (напр. «↩️ Скасувати») — оголошення ще актуальні
    return True


def list_archived_watches(chat_id=None):
    """Товари в архіві (усіх або одного чату), найновіші в архіві першими."""
    q = "SELECT * FROM watches WHERE archived_at IS NOT NULL" + (" AND chat_id = ?" if chat_id is not None else "")
    with get_conn() as conn:
        rows = conn.execute(q + " ORDER BY archived_at DESC", (chat_id,) if chat_id is not None else ()).fetchall()
    return [dict(r) for r in rows]


def get_archived_watch(watch_id, chat_id):
    return next((w for w in list_archived_watches(chat_id) if w["id"] == watch_id), None)


def find_archived_watch(chat_id, query):
    """Товар в архіві з такою ж назвою пошуку (без урахування регістру й пробілів) або None."""
    normalized = " ".join(query.strip().lower().split())
    return next((w for w in list_archived_watches(chat_id)
                 if " ".join(w["query"].strip().lower().split()) == normalized), None)


def delete_archived_watch(watch_id, chat_id):
    """Видалити товар з архіву назавжди: історія цін і продажів стирається. True — видалено."""
    with get_conn() as conn:
        done = conn.execute("UPDATE watches SET archived_at = NULL WHERE id = ? AND chat_id = ? AND archived_at IS NOT NULL",
                            (watch_id, chat_id)).rowcount
        if done:
            _purge_watch_data(conn, watch_id)
    return bool(done)


def restore_watch(watch_id, chat_id):
    """Скасувати видалення: True, якщо історія ще збереглась і товар знову відстежується."""
    with get_conn() as conn:
        restored = conn.execute(
            "UPDATE watches SET active = 1, deleted_at = NULL "
            "WHERE id = ? AND chat_id = ? AND active = 0 AND deleted_at IS NOT NULL",
            (watch_id, chat_id),
        ).rowcount
        if restored:   # поки товар був видалений, ринок не скануввався — перерахувати
            conn.execute("UPDATE market_stats SET updated_at = 0 WHERE watch_id = ?", (watch_id,))
        return bool(restored)


def purge_deleted_watches(keep_seconds=None):
    """Остаточно прибирає історію товарів, видалених давніше за UNDO_KEEP_HOURS."""
    keep = settings.UNDO_KEEP_HOURS * 3600 if keep_seconds is None else keep_seconds
    cutoff = int(time.time()) - keep
    with get_conn() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM watches WHERE active = 0 AND deleted_at IS NOT NULL AND deleted_at <= ?",
            (cutoff,)).fetchall()]
        for wid in ids:
            _purge_watch_data(conn, wid)
            conn.execute("UPDATE watches SET deleted_at = NULL WHERE id = ?", (wid,))
        conn.execute("DELETE FROM undo_actions WHERE created_at <= ?", (cutoff,))
    return len(ids)


def mark_market_stale(watch_id):
    """Статистика лишається, але найближчий цикл перерахує ринок заново (≤5 хв)."""
    with get_conn() as conn:
        conn.execute("UPDATE market_stats SET updated_at = 0 WHERE watch_id = ?", (watch_id,))


def prune_listing_obs(watch_id, min_price=0, drop_unspecified=False):
    """Прибирає з історії товару лише те, що не проходить його НОВІ фільтри
    (решта спостережень і продажів лишається). Повертає кількість прибраних."""
    conds, params = [], [watch_id]
    if min_price:
        conds.append("price < ?")
        params.append(min_price)
    if drop_unspecified:
        conds.append("COALESCE(spec_group, 'unspecified') = 'unspecified'")
    if not conds:
        return 0
    with get_conn() as conn:
        return conn.execute(
            f"DELETE FROM listing_obs WHERE watch_id = ? AND ({' OR '.join(conds)})", params).rowcount


def reset_watch_market(watch_id):
    """Після зміни фільтрів (категорія, мін. ціна, стан, виключені слова)
    стара статистика й спостереження стосуються вже іншої вибірки."""
    with get_conn() as conn:
        conn.execute("DELETE FROM market_stats WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM listing_obs WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM price_history WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM scan_stats WHERE watch_id = ?", (watch_id,))


def reset_active_listings(watch_id):
    """Після зміни категорій: продажі й зниклі лишаються (вони вже пройшли фільтри), а поточні
    оголошення збираються заново — інакше лоти з прибраної категорії «зникли б» з пошуку
    і бот прийняв би їх за продані. Повертає кількість прибраних активних."""
    with get_conn() as conn:
        removed = conn.execute("DELETE FROM listing_obs WHERE watch_id = ? AND status = 'active'",
                               (watch_id,)).rowcount
        conn.execute("DELETE FROM market_stats WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM scan_stats WHERE watch_id = ?", (watch_id,))
    return removed


def update_watch_exclude(watch_id, chat_id, exclude_text):
    with get_conn() as conn:
        conn.execute(
            "UPDATE watches SET exclude = ? WHERE id = ? AND chat_id = ?",
            (exclude_text, watch_id, chat_id),
        )


def update_watch_conditions(watch_id, chat_id, condition_ids):
    with get_conn() as conn:
        conn.execute(
            "UPDATE watches SET condition_ids = ? WHERE id = ? AND chat_id = ?",
            (condition_ids, watch_id, chat_id),
        )


def get_watch_categories(w):
    """Категорії товару: [{"id", "name"}]. Старі товари з однією категорією
    (лише category_id) теж розуміються."""
    raw = (w or {}).get("categories_json")
    if raw:
        try:
            cats = json.loads(raw)
            if isinstance(cats, list):
                return [c for c in cats if c.get("id")]
        except ValueError:
            pass
    if (w or {}).get("category_id"):
        return [{"id": w["category_id"], "name": w.get("category_name") or w["category_id"]}]
    return []


def watch_category_ids(w):
    return [c["id"] for c in get_watch_categories(w)]


def update_watch_categories(watch_id, chat_id, categories):
    """Зберігає список категорій; перша дублюється в category_id/name для сумісності."""
    first = categories[0] if categories else {"id": "", "name": ""}
    with get_conn() as conn:
        conn.execute(
            """UPDATE watches SET categories_json = ?, category_id = ?, category_name = ?
               WHERE id = ? AND chat_id = ?""",
            (json.dumps(categories, ensure_ascii=False) if categories else None,
             first["id"], first["name"], watch_id, chat_id),
        )


def update_watch_require_spec(watch_id, chat_id, value):
    """value: 1 — лише лоти з відомою пам'яттю, 0 — усі, None — автоматично."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE watches SET require_spec = ? WHERE id = ? AND chat_id = ?",
            (value, watch_id, chat_id),
        )


def get_required_aspects(w):
    """Обов'язкові характеристики товару. У БД — JSON-список; старе
    значення з однією назвою (до підтримки кількох) теж розуміється."""
    raw = (w or {}).get("required_aspect")
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return [raw]
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def encode_required_aspects(names):
    return json.dumps(list(names), ensure_ascii=False) if names else None


def update_watch_required_aspect(watch_id, chat_id, required_aspect, require_spec):
    with get_conn() as conn:
        conn.execute(
            "UPDATE watches SET required_aspect = ?, require_spec = ? WHERE id = ? AND chat_id = ?",
            (required_aspect, require_spec, watch_id, chat_id),
        )


def update_watch_min_price(watch_id, chat_id, min_price):
    with get_conn() as conn:
        conn.execute(
            "UPDATE watches SET min_price = ? WHERE id = ? AND chat_id = ?",
            (float(min_price or 0), watch_id, chat_id),
        )


def upsert_market_stats(watch_id, cond_group, spec_group, median_price, sample_size,
                        sale_price=None, sale_source=""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO market_stats (watch_id, cond_group, spec_group, median_price, prev_median_price,
                                         sample_size, updated_at, sale_price, sale_source)
               VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)
               ON CONFLICT(watch_id, cond_group, spec_group) DO UPDATE SET
                 prev_median_price=market_stats.median_price,
                 median_price=excluded.median_price,
                 sample_size=excluded.sample_size,
                 updated_at=excluded.updated_at,
                 sale_price=excluded.sale_price,
                 sale_source=excluded.sale_source""",
            (watch_id, cond_group, spec_group, median_price, sample_size, int(time.time()),
             sale_price, sale_source),
        )


def get_last_prices_update(chat_id):
    """Коли востаннє оновлювались ринкові ціни товарів цього чату (timestamp або None)."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT MAX(ms.updated_at) AS t FROM market_stats ms
               JOIN watches w ON w.id = ms.watch_id WHERE w.chat_id = ? AND w.active = 1""",
            (chat_id,),
        ).fetchone()
    return row["t"] if row and row["t"] else None


def update_sale_price(watch_id, cond_group, spec_group, sale_price, sale_source):
    """Лише ціна продажу групи (без зсуву «попередньої» типової ціни)."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE market_stats SET sale_price = ?, sale_source = ?
               WHERE watch_id = ? AND cond_group = ? AND spec_group = ?""",
            (sale_price, sale_source, watch_id, cond_group, spec_group),
        )


def get_auto_min_price(watch_id, pct=None):
    """
    Автоматична мінімальна ціна для пошуку, якщо користувач свою не задав:
    pct% від найнижчої типової ціни товару (округлено до 5€). Так eBay сам не
    віддає чохли, кабелі й запчастини, і справжні оголошення не губляться серед
    них у великих категоріях.

    Поки ціни ще не пораховані (буває якраз тоді, коли справжні оголошення
    тонуть серед аксесуарів), беремо медіану тих справжніх оголошень, які бот
    уже встиг побачити (від 3 шт.). None — якщо даних ще зовсім немає.
    """
    pct = settings.MIN_PRICE_SUGGESTION_PCT if pct is None else pct
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MIN(median_price) AS m FROM market_stats WHERE watch_id = ? AND cond_group != 'parts'",
            (watch_id,),
        ).fetchone()
        base = row["m"] if row and row["m"] else None
        if base is None:
            prices = sorted(r["price"] for r in conn.execute(
                """SELECT price FROM listing_obs WHERE watch_id = ? AND status = 'active'
                   AND price IS NOT NULL AND cond_group != 'parts'""", (watch_id,)).fetchall())
            if len(prices) >= 3:
                base = prices[len(prices) // 2]
    if not base:
        return None
    return max(5, round(base * pct / 100 / 5) * 5)


def get_market_stats(watch_id):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM market_stats WHERE watch_id = ?", (watch_id,)
        ).fetchall()]


def delete_market_stats_except(watch_id, keep_keys):
    """Прибирає групи, яких немає в новому розрахунку (решта оновлюється
    через upsert і зберігає попередню медіану для стрілки тренду)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT cond_group, spec_group FROM market_stats WHERE watch_id = ?", (watch_id,)
        ).fetchall()
        for r in rows:
            if (r["cond_group"], r["spec_group"]) not in keep_keys:
                conn.execute(
                    "DELETE FROM market_stats WHERE watch_id = ? AND cond_group = ? AND spec_group = ?",
                    (watch_id, r["cond_group"], r["spec_group"]),
                )


def clear_market_stats(watch_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM market_stats WHERE watch_id = ?", (watch_id,))


def get_seen_items(watch_id, item_ids):
    """Записи "вже бачили" для багатьох лотів одним запитом → {item_id: row}."""
    result = {}
    ids = [i for i in item_ids if i]
    with get_conn() as conn:
        for start in range(0, len(ids), 500):  # обмеження SQLite на кількість параметрів
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT * FROM seen_items WHERE watch_id = ? AND item_id IN ({placeholders})",
                [watch_id, *chunk],
            ):
                result[row["item_id"]] = dict(row)
    return result


def bulk_upsert_seen_items(watch_id, rows):
    """
    rows: [(item_id, price, notified_price або None)] — одна транзакція на весь цикл.
    Ключ — (item_id, watch_id): той самий лот може бути в кількох відстеженнях.
    notified_price задається, коли за цю ціну щойно надіслано сповіщення;
    None — попереднє значення зберігається (щоб не сповіщати двічі про ту
    саму ціну, але сповістити знову, якщо ціна впаде ще нижче).
    """
    if not rows:
        return
    now = int(time.time())
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO seen_items (item_id, watch_id, first_seen_at, last_price, last_notified_price, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(item_id, watch_id) DO UPDATE SET
                 last_price = excluded.last_price,
                 last_notified_price = COALESCE(excluded.last_notified_price, seen_items.last_notified_price),
                 last_seen_at = excluded.last_seen_at""",
            [(item_id, watch_id, now, price, notified, now) for item_id, price, notified in rows],
        )


def upsert_seen_item(item_id, watch_id, price, notified_price=None):
    bulk_upsert_seen_items(watch_id, [(item_id, price, notified_price)])


def cleanup_old_seen_items():
    """Прибирає лоти, яких бот не бачив SEEN_ITEMS_RETENTION_DAYS днів (рахуємо
    від ОСТАННЬОЇ появи — інакше оголошення, що висить понад місяць, знову
    прийшло б як "нове"), старі непотрібні знахідки й старий лічильник запитів."""
    now = int(time.time())
    with get_conn() as conn:
        removed = conn.execute(
            "DELETE FROM seen_items WHERE COALESCE(last_seen_at, first_seen_at) < ?",
            (now - SEEN_ITEMS_RETENTION_DAYS * 86400,),
        ).rowcount
        conn.execute(
            "DELETE FROM deals WHERE status != 'bought' AND created_at < ?",
            (now - DEALS_RETENTION_DAYS * 86400,),
        )
        oldest_day = datetime.fromtimestamp(now - API_USAGE_RETENTION_DAYS * 86400, timezone.utc).strftime("%Y-%m-%d")
        conn.execute("DELETE FROM api_usage WHERE day < ?", (oldest_day,))
        return removed


# ---------- 🗂 Папки товарів ----------

FOLDER_NAME_MAX = 30


def list_folders(chat_id):
    """Власні папки користувача: [{'id', 'name'}] у порядку створення."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, name FROM watch_folders WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()]


def add_folder(chat_id, name):
    """Нова папка (або вже наявна з такою ж назвою). Повертає її id."""
    name = " ".join(name.split())[:FOLDER_NAME_MAX]
    for f in list_folders(chat_id):
        if f["name"].lower() == name.lower():
            return f["id"]
    with get_conn() as conn:
        return conn.execute("INSERT INTO watch_folders (chat_id, name, created_at) VALUES (?, ?, ?)",
                            (chat_id, name, int(time.time()))).lastrowid


def set_watch_folder(watch_id, folder):
    """folder — ключ папки («laptops», «c3» — власна №3) або None — автоматично."""
    with get_conn() as conn:
        conn.execute("UPDATE watches SET folder = ? WHERE id = ?", (folder, watch_id))


def drop_empty_folders(chat_id):
    """Власні папки, в яких не лишилось жодного товару (ні активного, ні в архіві), — прибрати."""
    with get_conn() as conn:
        conn.execute("""DELETE FROM watch_folders WHERE chat_id = ? AND NOT EXISTS
                        (SELECT 1 FROM watches w WHERE (w.active = 1 OR w.archived_at IS NOT NULL)
                         AND w.folder = 'c' || watch_folders.id)""",
                     (chat_id,))


# ---------- «🔎 Переглянути оголошення»: без старих оголошень ----------

LISTING_AGE_CHOICES = (30, 60, 100, 180)


def get_old_listing_filter(chat_id):
    """(увімкнено, межа в днях) — ховати оголошення, що висять довше за межу."""
    try:
        days = int(get_meta(f"old_days:{chat_id}") or 100)
    except ValueError:
        days = 100
    return get_meta(f"hide_old:{chat_id}") == "1", days


def set_old_listing_filter(chat_id, enabled=None, days=None):
    if enabled is not None:
        set_meta(f"hide_old:{chat_id}", "1" if enabled else "0")
    if days is not None:
        set_meta(f"old_days:{chat_id}", int(days))


# ---------- категорії-підказки (запасна підказка порогу, коли eBay недоступний) ----------


# Імпорти з інших частин бази — унизу, щоб модулі могли посилатися один на одного
from core.db.base import get_conn, get_meta, set_meta  # noqa: E402
