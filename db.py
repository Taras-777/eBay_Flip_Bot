"""
База даних SQLite: схема, міграції й усі операції читання/запису.
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import settings
from settings import (
    API_USAGE_RETENTION_DAYS,
    CATEGORY_ASPECTS_CACHE_DAYS,
    DEALS_RETENTION_DAYS,
    DEFAULT_DISCOUNT_THRESHOLD_PCT,
    GONE_MAX_LISTING_DAYS,
    GONE_MISS_THRESHOLD,
    LISTING_OBS_RETENTION_DAYS,
    MARKET_REFRESH_MINUTES,
    SEED_CATEGORY_HINTS,
    SEEN_ITEMS_RETENTION_DAYS,
    SOLD_LOOKBACK_DAYS,
    log,
)


# ============================================================
# БАЗА ДАНИХ (SQLite)
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    query TEXT NOT NULL,
    exclude TEXT DEFAULT '',
    condition_ids TEXT DEFAULT '',
    discount_threshold_pct REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    category_id TEXT DEFAULT '',
    category_name TEXT DEFAULT '',
    min_price REAL DEFAULT 0,
    require_spec INTEGER,
    required_aspect TEXT,
    categories_json TEXT
);

CREATE TABLE IF NOT EXISTS market_stats (
    watch_id INTEGER NOT NULL,
    cond_group TEXT NOT NULL,
    spec_group TEXT NOT NULL DEFAULT 'unspecified',
    median_price REAL NOT NULL,
    prev_median_price REAL,
    sample_size INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    sale_price REAL,
    sale_source TEXT DEFAULT '',
    PRIMARY KEY (watch_id, cond_group, spec_group)
);

-- Кеш конфігурації лота, визначеної з його характеристик (getItem)
CREATE TABLE IF NOT EXISTS item_specs (
    item_id TEXT PRIMARY KEY,
    spec_group TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    aspects_json TEXT
);

-- Кеш характеристик категорії з Taxonomy API
CREATE TABLE IF NOT EXISTS category_aspects (
    category_id TEXT PRIMARY KEY,
    aspects_json TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);

-- Власний лічильник запитів до eBay за добу (UTC)
CREATE TABLE IF NOT EXISTS api_usage (
    day TEXT NOT NULL,
    api TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, api)
);

-- Спостереження за оголошеннями (дані рівня лота, без даних продавця):
-- потрібні, щоб помітити лоти, які зникли з видачі (ймовірно продані)
CREATE TABLE IF NOT EXISTS listing_obs (
    watch_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    cond_group TEXT,
    spec_group TEXT,
    price REAL,
    created_at INTEGER,
    end_at INTEGER,
    first_seen INTEGER,
    last_seen INTEGER,
    miss_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    gone_at INTEGER,
    title TEXT,
    url TEXT,
    PRIMARY KEY (watch_id, item_id)
);

CREATE TABLE IF NOT EXISTS seen_items (
    item_id TEXT NOT NULL,
    watch_id INTEGER NOT NULL,
    first_seen_at INTEGER NOT NULL,
    last_price REAL,
    last_notified_price REAL,
    last_seen_at INTEGER,
    PRIMARY KEY (item_id, watch_id)
);

CREATE TABLE IF NOT EXISTS category_hints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keywords TEXT NOT NULL,
    pct REAL NOT NULL,
    reason TEXT DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    title TEXT NOT NULL,
    total_price REAL NOT NULL,
    currency TEXT NOT NULL,
    median_price REAL NOT NULL,
    discount_pct REAL NOT NULL,
    url TEXT NOT NULL,
    suspicious INTEGER NOT NULL DEFAULT 0,
    has_best_offer INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    username TEXT,
    first_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at INTEGER NOT NULL,
    decided_at INTEGER
);

"""


@contextmanager
def get_conn():
    # timeout: якщо базу саме пише інший потік — почекати, а не падати з
    # "database is locked"; WAL дозволяє читати під час запису
    conn = sqlite3.connect(settings.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")  # зберігається у файлі бази
        # Міграції: якщо таблиця вже існує зі старою структурою (без
        # нових колонок), перестворюємо її — це лише кеш статистики й
        # трекінгу, дані відновлюються самі на наступній перевірці.
        for table, required_col in [("market_stats", "prev_median_price"),
                                     ("seen_items", "last_price")]:
            existing = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if existing:
                cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if required_col not in cols:
                    conn.execute(f"DROP TABLE {table}")

        # Окрема міграція: у старій версії seen_items мала PRIMARY KEY
        # лише на item_id — якщо один і той самий eBay-лот потрапляв у
        # результати ДВОХ різних watches (напр. "iPhone 13" і "iPhone 13
        # 128GB"), трекінг ціни одного watch помилково "забирав" запис
        # у іншого. Правильний ключ — (item_id, watch_id) разом.
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='seen_items'"
        ).fetchone()
        if existing:
            pk_cols = [r["name"] for r in conn.execute("PRAGMA table_info(seen_items)").fetchall() if r["pk"] > 0]
            if pk_cols != ["item_id", "watch_id"]:
                conn.execute("DROP TABLE seen_items")

        # Бот заявлений в eBay як "exempt" від Marketplace Account
        # Deletion, тобто не зберігає даних користувачів eBay. Прибираємо
        # username продавців, які могли лишитись від попередніх версій.
        conn.execute("DROP TABLE IF EXISTS blacklisted_sellers")
        deals_cols = {r["name"] for r in conn.execute("PRAGMA table_info(deals)").fetchall()}
        if "seller_username" in deals_cols:
            conn.execute("UPDATE deals SET seller_username = NULL")
            try:
                conn.execute("ALTER TABLE deals DROP COLUMN seller_username")
            except sqlite3.OperationalError:
                pass  # стара версія SQLite — колонка лишається, але вже порожня

        conn.executescript(SCHEMA)

        seen_cols = {r["name"] for r in conn.execute("PRAGMA table_info(seen_items)").fetchall()}
        if seen_cols and "last_seen_at" not in seen_cols:
            conn.execute("ALTER TABLE seen_items ADD COLUMN last_seen_at INTEGER")

        obs_cols = {r["name"] for r in conn.execute("PRAGMA table_info(listing_obs)").fetchall()}
        for col in ("title", "url"):
            if obs_cols and col not in obs_cols:
                conn.execute(f"ALTER TABLE listing_obs ADD COLUMN {col} TEXT")

        spec_cols = {r["name"] for r in conn.execute("PRAGMA table_info(item_specs)").fetchall()}
        if "aspects_json" not in spec_cols:
            conn.execute("ALTER TABLE item_specs ADD COLUMN aspects_json TEXT")

        ms_cols = {r["name"] for r in conn.execute("PRAGMA table_info(market_stats)").fetchall()}
        for col, ddl in [("sale_price", "REAL"), ("sale_source", "TEXT DEFAULT ''")]:
            if col not in ms_cols:
                conn.execute(f"ALTER TABLE market_stats ADD COLUMN {col} {ddl}")

        # Нові колонки watches (категорія eBay і мінімальна ціна) —
        # додаємо до вже наявної таблиці, не чіпаючи збережені товари
        watch_cols = {r["name"] for r in conn.execute("PRAGMA table_info(watches)").fetchall()}
        for col, ddl in [
            ("category_id", "TEXT DEFAULT ''"),
            ("category_name", "TEXT DEFAULT ''"),
            ("min_price", "REAL DEFAULT 0"),
            ("require_spec", "INTEGER"),
            ("required_aspect", "TEXT"),
            ("categories_json", "TEXT"),
        ]:
            if col not in watch_cols:
                conn.execute(f"ALTER TABLE watches ADD COLUMN {col} {ddl}")

        row = conn.execute("SELECT COUNT(*) AS c FROM category_hints").fetchone()
        if row["c"] == 0:
            for keywords, pct, reason in SEED_CATEGORY_HINTS:
                conn.execute(
                    "INSERT INTO category_hints (keywords, pct, reason, created_at) VALUES (?, ?, ?, ?)",
                    (keywords, pct, reason, int(time.time())),
                )


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


def list_users(status=None):
    q = "SELECT * FROM users"
    params = []
    if status:
        q += " WHERE status = ?"
        params.append(status)
    q += " ORDER BY requested_at DESC"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


# ---------- watches ----------

def add_watch(chat_id, label, query, exclude, condition_ids, discount_threshold_pct,
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


def remove_watch(watch_id, chat_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE watches SET active = 0 WHERE id = ? AND chat_id = ?",
            (watch_id, chat_id),
        )
        conn.execute("DELETE FROM market_stats WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM seen_items WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM listing_obs WHERE watch_id = ?", (watch_id,))


def reset_watch_market(watch_id):
    """Після зміни фільтрів (категорія, мін. ціна, стан, виключені слова)
    стара статистика й спостереження стосуються вже іншої вибірки."""
    with get_conn() as conn:
        conn.execute("DELETE FROM market_stats WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM listing_obs WHERE watch_id = ?", (watch_id,))


def update_threshold(watch_id, chat_id, new_pct):
    with get_conn() as conn:
        conn.execute(
            "UPDATE watches SET discount_threshold_pct = ? WHERE id = ? AND chat_id = ?",
            (new_pct, watch_id, chat_id),
        )


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


# ---------- спостереження за лотами ("зниклі" = ймовірно продані) ----------

def update_listing_observations(watch_id, items, window_start=None, present_ids=None):
    """
    Оновлює спостереження після ринкового сканування (N найновіших лотів,
    sort=newlyListed). Лот, створений ПІЗНІШЕ за найстаріший лот поточної
    видачі, мав би в ній бути; якщо його немає GONE_MISS_THRESHOLD разів
    поспіль — він зник (ймовірно куплений). Лоти, старші за вікно
    видачі, не оцінюємо: вони могли просто вийти за межі N найновіших.
    """
    now = int(time.time())
    # Присутні — усі лоти видачі (зокрема тимчасово відфільтровані), щоб не
    # прийняти за "зниклий" лот, який просто не пройшов фільтр цього разу
    present = set(present_ids) if present_ids else {it["item_id"] for it in items if it.get("item_id")}
    if window_start is None:
        created = [it["created_at"] for it in items if it.get("created_at")]
        window_start = min(created) if created else None

    with get_conn() as conn:
        for it in items:
            if not it.get("item_id"):
                continue
            conn.execute(
                """INSERT INTO listing_obs (watch_id, item_id, cond_group, spec_group, price,
                                            created_at, end_at, first_seen, last_seen, miss_count, status,
                                            title, url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'active', ?, ?)
                   ON CONFLICT(watch_id, item_id) DO UPDATE SET
                     cond_group=excluded.cond_group, spec_group=excluded.spec_group,
                     price=excluded.price, end_at=excluded.end_at, last_seen=excluded.last_seen,
                     miss_count=0, status='active', gone_at=NULL,
                     title=excluded.title, url=excluded.url""",
                (watch_id, it["item_id"], it["cond_group"], it.get("spec_group", "unspecified"),
                 it["total_price"], it.get("created_at"), it.get("end_at"), now, now,
                 it.get("title"), it.get("url")),
            )

        if window_start is None:
            return
        rows = conn.execute(
            """SELECT item_id, miss_count, end_at, first_seen, created_at FROM listing_obs
               WHERE watch_id = ? AND status = 'active' AND created_at IS NOT NULL AND created_at >= ?""",
            (watch_id, window_start),
        ).fetchall()
        for r in rows:
            if r["item_id"] in present:
                continue
            misses = r["miss_count"] + 1
            if r["end_at"] and r["end_at"] <= now:
                status = "ended"  # закінчився строк оголошення — це не продаж
            elif misses >= GONE_MISS_THRESHOLD:
                age_days = (now - (r["created_at"] or r["first_seen"])) / 86400
                status = "gone" if age_days <= GONE_MAX_LISTING_DAYS else "ended"
            else:
                status = "active"
            conn.execute(
                """UPDATE listing_obs SET miss_count = ?, status = ?,
                     gone_at = CASE WHEN ? = 'gone' THEN ? ELSE gone_at END
                   WHERE watch_id = ? AND item_id = ?""",
                (misses, status, status, now, watch_id, r["item_id"]),
            )


def get_gone_prices(watch_id, cond_group, spec_group=None):
    """Ціни лотів, які зникли з видачі за останні SOLD_LOOKBACK_DAYS днів."""
    since = int(time.time()) - SOLD_LOOKBACK_DAYS * 86400
    q = "SELECT price FROM listing_obs WHERE watch_id = ? AND status = 'gone' AND gone_at >= ? AND cond_group = ?"
    params = [watch_id, since, cond_group]
    if spec_group is not None:
        q += " AND spec_group = ?"
        params.append(spec_group)
    with get_conn() as conn:
        return [r["price"] for r in conn.execute(q, params).fetchall() if r["price"]]


def get_current_listings(watch_id):
    """Активні оголошення, які бот бачив в останніх ринкових скануваннях."""
    since = int(time.time()) - 2 * MARKET_REFRESH_MINUTES * 60
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT item_id, cond_group, spec_group, price, title, url FROM listing_obs
               WHERE watch_id = ? AND status = 'active' AND last_seen >= ? AND price IS NOT NULL""",
            (watch_id, since),
        ).fetchall()]


def cleanup_old_listing_obs():
    cutoff = int(time.time()) - LISTING_OBS_RETENTION_DAYS * 86400
    with get_conn() as conn:
        conn.execute("DELETE FROM item_specs WHERE fetched_at < ?", (cutoff,))
        return conn.execute("DELETE FROM listing_obs WHERE last_seen < ?", (cutoff,)).rowcount


def get_cached_specs(item_ids):
    """item_id → (spec_group, aspects dict або None, якщо характеристики не збережені)."""
    if not item_ids:
        return {}
    with get_conn() as conn:
        placeholders = ",".join("?" * len(item_ids))
        rows = conn.execute(
            f"SELECT item_id, spec_group, aspects_json FROM item_specs WHERE item_id IN ({placeholders})",
            list(item_ids),
        ).fetchall()
    result = {}
    for r in rows:
        try:
            aspects = json.loads(r["aspects_json"]) if r["aspects_json"] else None
        except ValueError:
            aspects = None
        result[r["item_id"]] = (r["spec_group"], aspects)
    return result


def save_cached_spec(item_id, spec_group, aspects=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO item_specs (item_id, spec_group, fetched_at, aspects_json) VALUES (?, ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET spec_group=excluded.spec_group,
                 fetched_at=excluded.fetched_at, aspects_json=excluded.aspects_json""",
            (item_id, spec_group, int(time.time()),
             json.dumps(aspects, ensure_ascii=False) if aspects is not None else None),
        )


def get_cached_category_aspects(category_id):
    since = int(time.time()) - CATEGORY_ASPECTS_CACHE_DAYS * 86400
    with get_conn() as conn:
        row = conn.execute(
            "SELECT aspects_json FROM category_aspects WHERE category_id = ? AND fetched_at >= ?",
            (str(category_id), since),
        ).fetchone()
    return json.loads(row["aspects_json"]) if row else None


def save_category_aspects(category_id, aspects):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO category_aspects (category_id, aspects_json, fetched_at) VALUES (?, ?, ?)
               ON CONFLICT(category_id) DO UPDATE SET aspects_json=excluded.aspects_json,
                 fetched_at=excluded.fetched_at""",
            (str(category_id), json.dumps(aspects, ensure_ascii=False), int(time.time())),
        )


# ---------- категорії-підказки (запасна підказка порогу, коли eBay недоступний) ----------

def list_category_hints():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM category_hints ORDER BY id").fetchall()]


def find_threshold_suggestion(query: str):
    q_lower = query.lower()
    for hint in list_category_hints():
        keywords = [k.strip() for k in hint["keywords"].split(",") if k.strip()]
        for kw in keywords:
            if kw in q_lower:
                return hint["pct"], hint["reason"]
    return (
        DEFAULT_DISCOUNT_THRESHOLD_PCT,
        "для цього товару немає готової підказки в базі, тому запропоновано типове значення",
    )


# ---------- deals ----------

def add_deal(watch_id, item_id, title, total_price, currency, median_price, discount_pct, url, suspicious, has_best_offer=False):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO deals
               (watch_id, item_id, title, total_price, currency, median_price, discount_pct, url, suspicious, has_best_offer, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)""",
            (watch_id, item_id, title, total_price, currency, median_price, discount_pct, url,
             int(suspicious), int(has_best_offer), int(time.time())),
        )
        return cur.lastrowid


def set_deal_status(deal_id, status):
    with get_conn() as conn:
        conn.execute("UPDATE deals SET status = ? WHERE id = ?", (status, deal_id))


def get_deal_owner_chat_id(deal_id):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT w.chat_id FROM deals d
               JOIN watches w ON w.id = d.watch_id WHERE d.id = ?""",
            (deal_id,),
        ).fetchone()
        return row["chat_id"] if row else None


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