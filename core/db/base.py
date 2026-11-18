"""
З'єднання з базою, схема й міграції, службові значення (meta).
"""

import sqlite3
from contextlib import contextmanager

from core import settings


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
    categories_json TEXT,
    deleted_at INTEGER,         -- видалено, але дані ще зберігаються для «↩️ Скасувати»
    archived_at INTEGER         -- у «🗄 Архів»: не сканується, але історія й налаштування зберігаються
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
    aspects_json TEXT,
    -- з тієї самої відповіді (лише для історії): оголошення магазинів з кількома штуками
    est_sold INTEGER,       -- скільки штук уже продано (estimatedSoldQuantity)
    est_available INTEGER,  -- скільки лишилось
    availability_at INTEGER,
    bin_price REAL          -- ціна «купити зараз» для «аукціон + купити зараз» (Trading API)
);

-- Кеш характеристик категорії з Taxonomy API
CREATE TABLE IF NOT EXISTS category_aspects (
    category_id TEXT PRIMARY KEY,
    aspects_json TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);

-- «💡 Що перепродавати»: оголошення популярних товарів, яких ще ніхто не
-- відстежує (щоб рахувати, як швидко вони зникають), і готові результати
CREATE TABLE IF NOT EXISTS discovery_obs (
    candidate TEXT NOT NULL,
    item_id TEXT NOT NULL,
    price REAL,
    spec_group TEXT,
    cond_group TEXT,
    created_at INTEGER,
    first_seen INTEGER,
    last_seen INTEGER,
    status TEXT DEFAULT 'active',
    gone_at INTEGER,
    sold_check TEXT,      -- перевірка через Trading API, як у listing_obs
    checked_at INTEGER,
    PRIMARY KEY (candidate, item_id)
);

CREATE TABLE IF NOT EXISTS discovery_results (
    candidate TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Службові значення бота (напр. номер версії коду)
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
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
    sold_check TEXT,      -- перевірка через Trading API: pending/sold/unsold/active/unknown
    checked_at INTEGER,
    category_id TEXT,     -- кінцева категорія eBay, де виставлено оголошення
    category_name TEXT,
    -- лише для історії (на них поки не будуються функції):
    buying_options TEXT,  -- FIXED_PRICE, BEST_OFFER, AUCTION через кому
    condition_id TEXT,    -- точний стан eBay: 1000 Neu, 1500 Neu sonstige, 2000–2500 Generalüberholt, 3000 Gebraucht…
    shipping_cost REAL,   -- найдешевша доставка (у price — уже разом з нею)
    pickup_only INTEGER,  -- 1 — без доставки, лише самовивіз
    country TEXT,         -- країна, звідки відправляють (лише країна, без адреси)
    -- з перевірки продажу (та сама відповідь GetItem, без додаткових запитів):
    sold_price REAL,      -- фінальна ціна без доставки
    sold_at INTEGER,      -- точний час завершення
    bid_count INTEGER,    -- кількість ставок (оголошення «аукціон + купити зараз»)
    offer_count INTEGER,  -- скільки пропозицій ціни надіслали продавцю
    quantity INTEGER,     -- кількість штук в оголошенні (магазини)
    listing_type TEXT,    -- FixedPriceItem / Chinese (аукціон)
    watch_count INTEGER,  -- скільки людей стежили за оголошенням на момент перевірки
    current_bid REAL,     -- «аукціон + купити зараз»: поточна ставка
    PRIMARY KEY (watch_id, item_id)
);

-- Підсумок останнього ринкового сканування кожного товару (для головного меню)
CREATE TABLE IF NOT EXISTS scan_stats (
    watch_id INTEGER PRIMARY KEY,
    scanned_at INTEGER NOT NULL,
    fetched INTEGER NOT NULL,      -- скільки оголошень віддав eBay
    kept INTEGER NOT NULL,         -- скільки пройшли фільтри товару
    new_count INTEGER NOT NULL     -- скільки з них бот побачив уперше (додав у базу)
);

-- Типова ціна кожної групи по днях — для попереджень про падіння цін
CREATE TABLE IF NOT EXISTS price_history (
    watch_id INTEGER NOT NULL,
    cond_group TEXT NOT NULL,
    spec_group TEXT NOT NULL,
    day TEXT NOT NULL,
    median_price REAL NOT NULL,
    sale_price REAL,
    PRIMARY KEY (watch_id, cond_group, spec_group, day)
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

-- Лоти, які користувач прибрав: reason='wrong' — «🚫 Не той товар» (з них
-- бот вчить слова), 'hidden' — «🙈 Сховати» (той товар, але не підходить:
-- розбитий екран, пошкодження тощо). І ті, і ті більше не враховуються.
CREATE TABLE IF NOT EXISTS rejected_items (
    watch_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    title TEXT,
    created_at INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT 'wrong',
    PRIMARY KEY (watch_id, item_id)
);

-- Слова, які бот сам вивів із відхилених лотів: 'excluded' — додане до
-- виключених, 'ignored' — користувач скасував, більше не пропонувати
CREATE TABLE IF NOT EXISTS learned_words (
    watch_id INTEGER NOT NULL,
    word TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (watch_id, word)
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
    created_at INTEGER NOT NULL,
    seen_at INTEGER,       -- коли користувач побачив у «🔥 Вигідні пропозиції» (NULL — нова)
    cond_group TEXT,       -- група статистики (стан, конфігурація), за якою пропозиція вигідна
    spec_group TEXT,
    listed_at INTEGER,     -- коли оголошення виставили на eBay
    checked_at INTEGER,    -- коли востаннє перевіряли, чи воно ще продається
    item_spec TEXT,        -- власна конфігурація оголошення (spec_group — група, з якої взято ціну)
    sale_source TEXT,      -- на чому базується «продати»: «за 7 проданими» / «за поточними оголошеннями»
    sale_sample INTEGER,   -- скільки оголошень у групі
    watch_count INTEGER,   -- скільки людей стежать (з перевірки «ще продається?»)
    auction INTEGER DEFAULT 0,  -- «аукціон + купити зараз»
    current_bid REAL,      -- ставка на момент знахідки
    bid_count INTEGER,
    end_at INTEGER         -- коли закінчується
);

-- «📉 Знизили ціну»: перша й поточна ціна кожного оголошення товару
CREATE TABLE IF NOT EXISTS price_track (
    watch_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    title TEXT,
    url TEXT,
    cond_group TEXT,
    spec_group TEXT,
    listed_at INTEGER,          -- коли оголошення виставили на eBay
    has_best_offer INTEGER DEFAULT 0,
    first_price REAL NOT NULL,  -- ціна, коли бот побачив оголошення вперше
    first_seen INTEGER NOT NULL,
    price REAL NOT NULL,        -- поточна ціна (з доставкою)
    min_price REAL NOT NULL,
    price_changed_at INTEGER,   -- коли ціну востаннє знизили
    last_seen INTEGER NOT NULL, -- 0 — перевірили: продано чи знято
    seen_at INTEGER,            -- коли користувач бачив поточну знижку (NULL — нова)
    dismissed_price REAL,
    first_title TEXT,           -- назва й конфігурація, коли бот побачив оголошення вперше (якщо продавець
    first_spec TEXT,            -- переробив оголошення під інший товар — знижка рахується заново)
    PRIMARY KEY (watch_id, item_id)
);

-- «↩️ Скасувати»: що саме змінила дія, щоб повернути як було
CREATE TABLE IF NOT EXISTS undo_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
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


# Індекси: без них кожен екран і кожна перевірка переглядають усю таблицю
# спостережень, яка росте на тисячі рядків щодня. Створюються один раз.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_obs_watch_status ON listing_obs (watch_id, status, gone_at);
CREATE INDEX IF NOT EXISTS idx_obs_sold_check ON listing_obs (sold_check, gone_at);
CREATE INDEX IF NOT EXISTS idx_obs_item ON listing_obs (item_id);
CREATE INDEX IF NOT EXISTS idx_obs_last_seen ON listing_obs (last_seen);
CREATE INDEX IF NOT EXISTS idx_disc_candidate ON discovery_obs (candidate, status, gone_at);
CREATE INDEX IF NOT EXISTS idx_disc_sold_check ON discovery_obs (sold_check, gone_at);
CREATE INDEX IF NOT EXISTS idx_disc_item ON discovery_obs (item_id);
CREATE INDEX IF NOT EXISTS idx_deals_watch ON deals (watch_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_seen_watch ON seen_items (watch_id, item_id);
CREATE INDEX IF NOT EXISTS idx_watches_chat ON watches (chat_id, active);
CREATE INDEX IF NOT EXISTS idx_undo_chat ON undo_actions (chat_id, id);
CREATE INDEX IF NOT EXISTS idx_track_watch ON price_track (watch_id, last_seen);
CREATE INDEX IF NOT EXISTS idx_disc_first_seen ON discovery_obs (first_seen);
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at INTEGER NOT NULL,
    chat_id INTEGER,
    action TEXT NOT NULL,
    seconds REAL NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_log_at ON action_log (at);
CREATE TABLE IF NOT EXISTS manual_specs (
    item_id TEXT PRIMARY KEY,
    spec_group TEXT NOT NULL,
    at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS review_items (
    watch_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT,
    price REAL,
    url TEXT,
    note TEXT,
    at INTEGER NOT NULL,
    PRIMARY KEY (watch_id, item_id)
);
CREATE TABLE IF NOT EXISTS watch_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auction_alerts (
    chat_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    watch_id INTEGER NOT NULL,
    end_at INTEGER NOT NULL,
    buy_limit REAL NOT NULL,
    shipping REAL NOT NULL DEFAULT 0,
    bid REAL,
    text TEXT,
    url TEXT,
    message_id INTEGER,
    final_at INTEGER,
    at INTEGER NOT NULL,
    title TEXT,
    hidden INTEGER DEFAULT 0,
    checked_at INTEGER,
    bid_count INTEGER,
    PRIMARY KEY (chat_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_auction_end ON auction_alerts (end_at);
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
        conn.execute("DROP TABLE IF EXISTS notices")   # колишні «🔔 Повідомлення» — прибрано
        # Те саме оголошення кілька разів у «🔥» (після кожного зниження ціни) — лишаємо найновіший запис
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='deals'").fetchone():
            conn.execute("""DELETE FROM deals WHERE status = 'new' AND id NOT IN
                            (SELECT MAX(id) FROM deals WHERE status = 'new' GROUP BY watch_id, item_id)""")
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
        for col, ddl in [("title", "TEXT"), ("url", "TEXT"), ("sold_check", "TEXT"), ("checked_at", "INTEGER"),
                         ("category_id", "TEXT"), ("category_name", "TEXT"), ("buying_options", "TEXT"),
                         ("condition_id", "TEXT"), ("shipping_cost", "REAL"), ("pickup_only", "INTEGER"),
                         ("country", "TEXT"), ("sold_price", "REAL"), ("sold_at", "INTEGER"),
                         ("bid_count", "INTEGER"), ("offer_count", "INTEGER"), ("quantity", "INTEGER"),
                         ("listing_type", "TEXT"), ("watch_count", "INTEGER"), ("current_bid", "REAL"),
                         ("seller_type", "TEXT")]:
            if obs_cols and col not in obs_cols:
                conn.execute(f"ALTER TABLE listing_obs ADD COLUMN {col} {ddl}")

        track_cols = {r["name"] for r in conn.execute("PRAGMA table_info(price_track)").fetchall()}
        if track_cols and "dismissed_price" not in track_cols:
            # «🧹 Очистити список» у «📉 Знизили ціну»: ціна, за якої оголошення прибрали зі списку
            conn.execute("ALTER TABLE price_track ADD COLUMN dismissed_price REAL")
        if track_cols and "first_title" not in track_cols:
            # Перші назва й конфігурація: оголошення переробили під інший товар — знижку рахуємо заново
            conn.execute("ALTER TABLE price_track ADD COLUMN first_title TEXT")
            conn.execute("ALTER TABLE price_track ADD COLUMN first_spec TEXT")
            conn.execute("UPDATE price_track SET first_title = title, first_spec = spec_group")

        deal_cols = {r["name"] for r in conn.execute("PRAGMA table_info(deals)").fetchall()}
        if deal_cols and "seen_at" not in deal_cols:
            conn.execute("ALTER TABLE deals ADD COLUMN seen_at INTEGER")
            conn.execute("UPDATE deals SET seen_at = created_at")  # старі — вже бачені в чаті
        for col, ddl in [("cond_group", "TEXT"), ("spec_group", "TEXT"),
                         ("listed_at", "INTEGER"), ("checked_at", "INTEGER"),
                         ("item_spec", "TEXT"), ("sale_source", "TEXT"), ("sale_sample", "INTEGER"),
                         ("watch_count", "INTEGER"), ("auction", "INTEGER DEFAULT 0"), ("current_bid", "REAL"),
                         ("bid_count", "INTEGER"), ("end_at", "INTEGER")]:
            if deal_cols and col not in deal_cols:
                conn.execute(f"ALTER TABLE deals ADD COLUMN {col} {ddl}")

        disc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(discovery_obs)").fetchall()}
        for col, ddl in [("sold_check", "TEXT"), ("checked_at", "INTEGER")]:
            if disc_cols and col not in disc_cols:
                conn.execute(f"ALTER TABLE discovery_obs ADD COLUMN {col} {ddl}")

        # Лоти, що зникли ще до появи перевірки продажів (sold_check порожній), —
        # теж у чергу: eBay пам'ятає завершені оголошення до 90 днів
        conn.execute("UPDATE listing_obs SET sold_check = 'pending' "
                     "WHERE status = 'gone' AND sold_check IS NULL AND gone_at IS NOT NULL")
        conn.execute("UPDATE discovery_obs SET sold_check = 'pending' "
                     "WHERE status = 'gone' AND sold_check IS NULL AND gone_at IS NOT NULL")

        rej_cols = {r["name"] for r in conn.execute("PRAGMA table_info(rejected_items)").fetchall()}
        if rej_cols and "reason" not in rej_cols:
            conn.execute("ALTER TABLE rejected_items ADD COLUMN reason TEXT NOT NULL DEFAULT 'wrong'")

        spec_cols = {r["name"] for r in conn.execute("PRAGMA table_info(item_specs)").fetchall()}
        for col, ddl in (("est_sold", "INTEGER"), ("est_available", "INTEGER"), ("availability_at", "INTEGER"),
                         ("bin_price", "REAL")):
            if spec_cols and col not in spec_cols:
                conn.execute(f"ALTER TABLE item_specs ADD COLUMN {col} {ddl}")
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
            ("deleted_at", "INTEGER"),
            ("min_profit", "REAL"),   # свій мін. прибуток товару (NULL — загальний користувача)
            ("folder", "TEXT"),       # папка в «🗂 Категорії товарів», обрана вручну (NULL — автоматично)
            ("archived_at", "INTEGER"),   # у «🗄 Архів» (не сканується, дані зберігаються)
        ]:
            if col not in watch_cols:
                conn.execute(f"ALTER TABLE watches ADD COLUMN {col} {ddl}")

        conn.executescript(INDEXES)

        # «🔨 Аукціони» в меню: назва, приховані, коли востаннє перевіряли ставку
        auc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(auction_alerts)").fetchall()}
        for col, ddl in [("title", "TEXT"), ("hidden", "INTEGER DEFAULT 0"), ("checked_at", "INTEGER"),
                         ("bid_count", "INTEGER")]:
            if auc_cols and col not in auc_cols:
                conn.execute(f"ALTER TABLE auction_alerts ADD COLUMN {col} {ddl}")

        # Змінилась комісія eBay (напр. 15% → 0%) — оголошення, які бот уже бачив і визнав невигідними,
        # оцінюються заново: за новим розрахунком частина з них може бути вигідною
        fee = str(float(settings.EBAY_SELLING_FEES_PCT))
        row = conn.execute("SELECT value FROM meta WHERE key = 'fees_pct'").fetchone()
        if row is None or row["value"] != fee:
            conn.execute("DELETE FROM seen_items WHERE last_notified_price IS NULL")
            conn.execute("INSERT INTO meta (key, value) VALUES ('fees_pct', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (fee,))


# ---------- службові значення ----------

def get_meta(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_meta(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

