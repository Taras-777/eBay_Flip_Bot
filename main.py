import asyncio
import base64
import functools
import html
import logging
import re
import sqlite3
import statistics
import threading
import time
import uuid
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx логує кожен запит до Telegram на рівні INFO разом з повною URL-адресою,
# яка містить токен бота. Піднімаємо рівень до WARNING — токен не потрапляє в
# логи, а лог не засмічується рядком getUpdates кожні 10 секунд.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ebay_flip_bot")

# ============================================================
# НЕ-СЕКРЕТНІ НАЛАШТУВАННЯ
# ============================================================

EBAY_MARKETPLACE_ID = "EBAY_DE"
DB_PATH = "ebay_flip_bot.sqlite3"
DEFAULT_DISCOUNT_THRESHOLD_PCT = 25
MIN_SAMPLE_SIZE = 8
MIN_MODEL_SAMPLE_SIZE = 3  # лише для пропозиції мінімальної ціни
CHECK_INTERVAL_MINUTES = 10
MIN_ALLOWED_THRESHOLD_PCT = 5
MAX_ALLOWED_THRESHOLD_PCT = 70

MIN_SELLER_FEEDBACK_SCORE = 5
MIN_SELLER_FEEDBACK_PCT = 95.0

BEST_OFFER_ASSUMED_DISCOUNT_PCT = 10

# Орієнтовна сукупна комісія eBay за продаж (final value fee) + оплата
# (PayPal/картка). Реальний відсоток залежить від категорії товару,
# це спрощена оцінка для прикидки прибутку — не точний розрахунок.
EBAY_SELLING_FEES_PCT = 15
# Орієнтовна вартість відправки товару покупцю при перепродажу (DHL/Hermes)
RESALE_SHIPPING_EUR = 7
# Мінімальний прибуток у євро — дрібні угоди на 5-10€ не варті зусиль
MIN_PROFIT_EUR = 15
# "Реалістична ціна продажу" за пропозиціями = цей перцентиль цін
# (дешевше за 75% оголошень). Товар реально продається за ціною,
# конкурентною з найдешевшими пропозиціями, а не за медіаною.
SALE_PRICE_PERCENTILE = 25

# Ринковий аналіз (медіана, ціна продажу) — раз на годину і по більшій
# вибірці; пошук нових вигідних лотів — кожні CHECK_INTERVAL_MINUTES.
MARKET_REFRESH_MINUTES = 60
MARKET_SCAN_PAGES = 2          # 2 × 100 найновіших оголошень
DEAL_SCAN_LIMIT = 50

# Трекер "зниклих" лотів: лот, що зник з видачі задовго до кінця терміну,
# найімовірніше купили. Його остання ціна — наближення до ціни продажу.
GONE_MISS_THRESHOLD = 2        # зник у 2 ринкових перевірках поспіль
GONE_MAX_LISTING_DAYS = 30     # старші лоти не враховуємо (могли просто зняти)
SOLD_LOOKBACK_DAYS = 60
MIN_SOLD_SAMPLE = 5
LISTING_OBS_RETENTION_DAYS = 90

# Стани товару за замовчуванням при додаванні нового відстеження
DEFAULT_CONDITION_IDS = "1000,1500,2000,2500,3000"

# Пресети станів товару для /setconditions — не змушуємо користувача
# вручну вводити числові condition_ids з eBay Browse API
CONDITION_PRESETS = {
    "new": "1000,1500,2000,2500",  # новий / новий інший / сертифіковано чи продавцем відновлений
    "used": "3000",
    "both": DEFAULT_CONDITION_IDS,  # значення за замовчуванням при додаванні товару
}

# Скільки категорій показувати на вибір при додаванні товару
MAX_CATEGORY_OPTIONS = 6
# Пропонована мінімальна ціна = цей відсоток від медіани в обраній категорії.
# Консоль чи ноутбук майже ніколи не продаються дешевше за ~40% від
# типової ціни, а ігри, чохли й кабелі — майже завжди дешевші.
MIN_PRICE_SUGGESTION_PCT = 40

# Товар мають реально доставити в Німеччину — тому в кожен пошуковий
# запит додається вимога доставки саме в DE. Додатково лот обмежується
# розташуванням у Німеччині чи іншій країні ЄС.
#
# ПРИМІТКА: itemLocationCountry в eBay Browse API приймає РІВНО ОДНЕ
# значення країни (офіційна документація: "Only one country code
# value can be used for this filter") — список із кількох країн через
# "|" там не підтримується. Для "уся країна ЄС" є окремий, спеціально
# призначений фільтр itemLocationRegion, і EUROPEAN_UNION — валідне
# значення для маркетплейсу EBAY_DE.
DELIVERY_COUNTRY = "DE"
ITEM_LOCATION_REGION = "EUROPEAN_UNION"
# Поштовий індекс покупця — передається в заголовку X-EBAY-C-ENDUSERCTX,
# щоб eBay точніше рахував вартість і доступність доставки саме сюди
EBAY_BUYER_POSTAL_CODE = "76684"

SEED_CATEGORY_HINTS = [
    ("iphone, samsung galaxy, pixel", 20, "смартфони — великий обсяг однорідних лотів"),
    ("playstation, ps5, ps4, xbox, nintendo switch", 20, "ігрові консолі — стандартизований товар"),
    ("macbook, thinkpad, dell xps, laptop, ноутбук", 25, "ноутбуки — конфігурації відрізняються"),
    ("airpods, sony wh, навушники, headphones", 20, "аудіо-гаджети — стабільна ринкова ціна"),
    ("nike, adidas, jordan, yeezy, кросівки, sneakers", 35, "взуття/одяг — розкид через розмір/стан"),
    ("watch, годинник, rolex, omega", 30, "годинники — розкид через стан/комплектацію"),
]

def condition_group(condition_text: str) -> str:
    """
    eBay повертає ЛЮДИНОЧИТАЄМИЙ текст стану, не enum-константу —
    наприклад "New other (see details)" чи "Certified - Refurbished"
    (з дужками й тире), а не "NEW_OTHER"/"CERTIFIED_REFURBISHED". Точне
    співставлення після UPPER+replace(" ","_") майже ніколи не
    спрацьовувало б для чогось складнішого за просте "New"/"Used" —
    тому шукаємо ключові слова, а не точний збіг рядка.
    """
    if not condition_text:
        return "unknown"
    c = condition_text.lower()
    if "for parts" in c or "not working" in c:
        return "unknown"  # свідомо не new і не used — окремо не групуємо
    if "used" in c or "gebraucht" in c:
        return "used"
    if (
        "new" in c
        or "refurbished" in c
        or "neu" in c
        or "neuwertig" in c
        or "generalГјberholt" in c
        or "generaluberholt" in c
        or "zertifiziert" in c
    ):
        return "new"
    return "unknown"


def condition_group_from_item(it):
    """
    Група стану за числовим conditionId з відповіді eBay — він однаковий
    для всіх мов ("Gebraucht" і "Used" мають той самий код 3000).
    1000–2999: новий / відновлений, 3000–6999: вживаний, 7000: на запчастини.
    Текст стану — лише запасний варіант, якщо коду немає.
    """
    try:
        cid = int(it.get("conditionId"))
    except (TypeError, ValueError):
        return condition_group(it.get("condition", ""))
    if cid >= 7000:
        return "parts"
    if cid >= 3000:
        return "used"
    return "new"


# Виявлення обсягу пам'яті/накопичувача прямо з назви оголошення.
# iPhone 12 256GB і iPhone 12 512GB мають РІЗНІ справедливі ціни —
# якщо рахувати одну медіану на весь запит "iPhone 12", вона вийде
# змішаною і однаково неточною для обох варіантів. Тому кожен лот
# додатково групується за цим "spec_key", і порівняння з медіаною
# відбувається лише в межах однакової конфігурації.
SPEC_SIZE_PATTERN = re.compile(r"(\d+)\s?(GB|TB)\b", re.IGNORECASE)
SEARCH_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)

CONSOLE_QUERY_TERMS = {
    "ps", "ps4", "ps5", "playstation", "xbox", "switch", "nintendo",
}
CONSOLE_ACCESSORY_TERMS = {
    "accessory", "accessories", "case", "cable", "controller", "cover",
    "game", "games", "hülle", "hulle", "kabel", "spiel", "spiele", "tasche",
    "zubehör", "zubehor", "charging", "adapter", "stand", "skin",
    "replacement", "ersatz", "repair", "reparatur", "teil", "teile",
    "parts", "defekt", "broken", "hdmi", "dock", "shell", "fan",
    "download", "code", "key", "voucher", "manual", "box",
    "dummy", "replica", "faceplate", "headset", "charger", "charging",
}
CONSOLE_PRODUCT_TERMS = {
    "console", "konsol", "konsole", "system", "slim", "pro",
    "digital", "disc", "bundle", "standard",
}
CONSOLE_GAME_TERMS = {
    "game", "games", "spiel", "spiele", "pal", "usk", "deluxe",
    "allstars", "squire", "fifa", "minecraft",
}
CONSOLE_ACCESSORY_ONLY_TERMS = {
    "code", "key", "voucher", "download", "manual", "box", "empty",
    "controller", "headset", "charger", "charging", "cable", "adapter",
    "case", "cover", "skin", "stand", "dock", "shell", "faceplate",
    "parts", "repair", "replacement", "fan", "hdmi",
}
LAPTOP_QUERY_TERMS = {
    "laptop", "notebook", "notebooks", "ноутбук", "ноутбуки",
    "macbook", "thinkpad", "latitude", "inspiron", "vostro",
    "precision", "xps", "ideapad", "zenbook", "vivobook",
    "chromebook", "surface",
}
LAPTOP_ACCESSORY_TERMS = {
    "keyboard", "tastatur", "touchpad", "trackpad", "speaker", "lautsprecher",
    "fan", "lüfter", "cable", "kabel", "charger", "netzteil", "adapter",
    "battery", "akku", "screen", "display", "lcd", "hinge", "scharnier",
    "case", "cover", "shell", "palmrest", "motherboard", "mainboard",
    "heatsink", "dock", "docking", "part", "parts", "replacement", "repair",
    "spare", "button", "bezel", "karte", "card", "connector", "io",
}
LAPTOP_HARD_ACCESSORY_TERMS = {
    "keyboard", "tastatur", "touchpad", "trackpad", "speaker", "lautsprecher",
    "fan", "lüfter", "cable", "kabel", "charger", "netzteil", "adapter",
    "battery", "akku", "screen", "display", "lcd", "hinge", "scharnier",
    "case", "cover", "shell", "palmrest", "motherboard", "mainboard",
    "heatsink", "dock", "docking", "part", "parts", "replacement", "repair",
    "spare", "button", "bezel", "connector",
}
LAPTOP_PRODUCT_TERMS = {
    "laptop", "notebook", "macbook", "thinkpad", "chromebook", "ultrabook",
    "computer", "pc", "ram", "ssd", "nvme", "intel", "ryzen", "core",
}
LAPTOP_DEVICE_TERMS = {
    "laptop", "notebook", "macbook", "thinkpad", "chromebook", "ultrabook",
    "computer", "pc",
}
# Слова, що позначають ІНШУ модель з помітно іншою ціною: PS5 vs PS5 Pro,
# iPhone 13 vs 13 Pro Max, Switch vs Switch Lite/OLED. Якщо слова немає в
# запиті — лоти з ним у назві не враховуються, і навпаки.
MODEL_VARIANT_TERMS = {"pro", "max", "plus", "mini", "lite", "oled"}
# "Windows 11 Pro" / "Win10 Pro" у назві ноутбука — це версія ОС, а не
# модель пристрою; такий "pro" не має впливати на перевірку варіанта.
OS_EDITION_PATTERN = re.compile(r"\b(?:windows|win)\s?(?:1[01])?\s?(?:pro|home)\b", re.IGNORECASE)
CURRENCY_TO_EUR = {
    "EUR": 1.0,
    "GBP": 1.17,
    "USD": 0.92,
    "CHF": 1.05,
    "PLN": 0.23,
    "CZK": 0.040,
}


def _search_tokens(text: str):
    return {token.casefold() for token in SEARCH_TOKEN_PATTERN.findall(text or "")}


def _title_matches_search(title: str, query: str, exclude_terms: str) -> bool:
    title_tokens = _search_tokens(title)
    query_tokens = _search_tokens(query)
    excluded_tokens = _search_tokens(exclude_terms)

    # eBay treats "PS 5" as a fuzzy query and can fill the result with
    # accessories. Require the console marker and reject accessory-only lots.
    normalized_title = "".join(title_tokens)
    normalized_query = "".join(query_tokens)
    is_ps5_query = (
        ("ps" in query_tokens and "5" in query_tokens)
        or "ps5" in query_tokens
        or {"playstation", "5"} <= query_tokens
        or {"play", "station", "5"} <= query_tokens
        or "playstation5" in normalized_query
    )
    if is_ps5_query:
        if (
            "ps5" not in title_tokens
            and "playstation5" not in normalized_title
            and not ({"playstation", "5"} <= title_tokens)
        ):
            return False
        required_variant_tokens = query_tokens - {"ps", "ps5", "playstation", "play", "station", "5"}
        if not required_variant_tokens.issubset(title_tokens):
            return False
        accessory_tokens = title_tokens.intersection(CONSOLE_ACCESSORY_TERMS)
        # A console category still contains games and other PS5-related
        # listings. Accept common console markers, storage capacity, or
        # Sony branding, but reject recognizable game listings.
        has_storage = bool(SPEC_SIZE_PATTERN.search(title))
        has_console_marker = bool(title_tokens.intersection(CONSOLE_PRODUCT_TERMS))
        has_sony_brand = "sony" in title_tokens
        if title_tokens.intersection(CONSOLE_GAME_TERMS):
            return False
        if title_tokens.intersection(CONSOLE_ACCESSORY_ONLY_TERMS):
            return False
        if not (has_console_marker or has_storage or has_sony_brand):
            return False
        # A console may be sold in a bundle with accessories, but an
        # accessory-only title must never become part of the market sample.
        if accessory_tokens and not has_console_marker:
            return False
    is_laptop_query = bool(query_tokens.intersection(LAPTOP_QUERY_TERMS))
    if is_laptop_query:
        has_laptop_marker = bool(title_tokens.intersection(LAPTOP_PRODUCT_TERMS))
        has_device_marker = bool(title_tokens.intersection(LAPTOP_DEVICE_TERMS))
        has_memory_or_storage = bool(
            re.search(r"\b\d+\s?(GB|TB)\b", title, re.IGNORECASE)
        )
        has_processor = bool(
            re.search(r"\b(i[3579]|ryzen|core|celeron|pentium)\b", title, re.IGNORECASE)
        )
        has_product_evidence = has_laptop_marker or has_memory_or_storage or has_processor
        hard_accessory_tokens = title_tokens.intersection(LAPTOP_HARD_ACCESSORY_TERMS)
        has_complete_laptop_evidence = (
            has_device_marker and (has_memory_or_storage or has_processor)
        ) or (has_memory_or_storage and has_processor)
        if hard_accessory_tokens and not has_complete_laptop_evidence:
            return False
        if title_tokens.intersection(LAPTOP_ACCESSORY_TERMS) and not has_product_evidence:
            return False
        if not has_product_evidence:
            return False
    if not is_ps5_query and query_tokens and not query_tokens.issubset(title_tokens):
        # Permit spacing differences such as "PS5" versus "PS 5".
        if normalized_query not in normalized_title:
            return False

    requested_variants = query_tokens.intersection(MODEL_VARIANT_TERMS)
    title_variant_tokens = _search_tokens(OS_EDITION_PATTERN.sub(" ", title))
    if title_variant_tokens.intersection(MODEL_VARIANT_TERMS) != requested_variants:
        return False

    return not title_tokens.intersection(excluded_tokens)


def extract_cpu_token(title: str):
    """
    Шукає в назві лота процесор — Apple Silicon, Intel Core чи AMD
    Ryzen — і повертає нормалізований токен, або None, якщо в назві
    процесор не згаданий взагалі. Формати позначення процесорів дуже
    різні (на відміну від GB/TB), тому це евристика: ловить
    найпоширеніші варіанти написання, але не гарантує 100% покриття.
    """
    if not title:
        return None

    # Apple Silicon: M1, M1 Pro, M2 Max, M3 Ultra, і майбутні M5/M6...
    m = re.search(r"\bM([1-9]\d?)\s?(Pro|Max|Ultra)?\b", title, re.IGNORECASE)
    if m:
        variant = (m.group(2) or "").upper()
        return f"M{m.group(1)}{variant}"

    # Intel Core Ultra (новіше позначення): Core Ultra 7 155H
    m = re.search(r"\bCore\s+Ultra\s+([3579])\s+(\d{3}[A-Za-z]{0,2})\b", title, re.IGNORECASE)
    if m:
        return f"COREULTRA{m.group(1)}-{m.group(2).upper()}"

    # Intel Core з конкретним номером моделі: i7-1165G7, i5 1135G7
    m = re.search(r"\bi([3579])[-\s]?(\d{3,5}[A-Za-z]{0,2}\d{0,2})\b", title, re.IGNORECASE)
    if m:
        return f"I{m.group(1)}-{m.group(2).upper()}"

    # AMD Ryzen з номером моделі: Ryzen 7 5800H
    m = re.search(r"\bRyzen\s?([3579])\s?(\d{3,4}[A-Z]{0,2})\b", title, re.IGNORECASE)
    if m:
        return f"RYZEN{m.group(1)}-{m.group(2).upper()}"

    # Без номера моделі — лише рівень (i7, Ryzen 5) як запасний варіант
    m = re.search(r"\bi([3579])\b", title, re.IGNORECASE)
    if m:
        return f"I{m.group(1)}"
    m = re.search(r"\bRyzen\s?([3579])\b", title, re.IGNORECASE)
    if m:
        return f"RYZEN{m.group(1)}"

    return None


def extract_spec_key(title: str) -> str:
    if not title:
        return "unspecified"

    tokens = {f"{num}{unit.upper()}" for num, unit in SPEC_SIZE_PATTERN.findall(title)}

    cpu = extract_cpu_token(title)
    if cpu:
        tokens.add(cpu)

    if not tokens:
        return "unspecified"
    return "+".join(sorted(tokens))


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
    min_price REAL DEFAULT 0
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
    PRIMARY KEY (watch_id, item_id)
);

CREATE TABLE IF NOT EXISTS seen_items (
    item_id TEXT NOT NULL,
    watch_id INTEGER NOT NULL,
    first_seen_at INTEGER NOT NULL,
    last_price REAL,
    last_notified_price REAL,
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
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
              category_id="", category_name="", min_price=0):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO watches
               (chat_id, label, query, exclude, condition_ids, discount_threshold_pct, active, created_at,
                category_id, category_name, min_price)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
            (chat_id, label, query, exclude, condition_ids, discount_threshold_pct, int(time.time()),
             category_id or "", category_name or "", float(min_price or 0)),
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


def update_watch_category(watch_id, chat_id, category_id, category_name):
    with get_conn() as conn:
        conn.execute(
            "UPDATE watches SET category_id = ?, category_name = ? WHERE id = ? AND chat_id = ?",
            (category_id or "", category_name or "", watch_id, chat_id),
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


def get_seen_item(item_id, watch_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM seen_items WHERE item_id = ? AND watch_id = ?", (item_id, watch_id)
        ).fetchone()
        return dict(row) if row else None


def upsert_seen_item(item_id, watch_id, price, notified_price=None):
    """
    Записує/оновлює ціну, за якою лот бачили востаннє В МЕЖАХ КОНКРЕТНОГО
    watch (один і той самий eBay-лот може потрапляти в результати
    кількох твоїх watches одночасно — напр. "iPhone 13" і "iPhone 13
    128GB" — тому ключ це (item_id, watch_id) разом, а не сам item_id).
    Якщо notified_price передано — це означає, що саме за цю ціну щойно
    надіслано сповіщення (використовується, щоб не сповіщати повторно
    про ту саму ціну, але сповістити знову, якщо вона впаде ще нижче).
    """
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT last_notified_price FROM seen_items WHERE item_id = ? AND watch_id = ?",
            (item_id, watch_id),
        ).fetchone()
        final_notified = notified_price if notified_price is not None else (
            existing["last_notified_price"] if existing else None
        )
        conn.execute(
            """INSERT INTO seen_items (item_id, watch_id, first_seen_at, last_price, last_notified_price)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(item_id, watch_id) DO UPDATE SET
                 last_price=excluded.last_price,
                 last_notified_price=?""",
            (item_id, watch_id, int(time.time()), price, final_notified, final_notified),
        )


# Скільки днів тримати запис "цей лот уже бачили", перш ніж прибрати
# з таблиці seen_items. Без цього таблиця росте нескінченно — старі
# лоти давно зникли з eBay і повторно все одно ніколи не з'являться,
# тому їх ідентифікатори більше не потрібні.
SEEN_ITEMS_RETENTION_DAYS = 30


def cleanup_old_seen_items():
    cutoff = int(time.time()) - SEEN_ITEMS_RETENTION_DAYS * 86400
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM seen_items WHERE first_seen_at < ?", (cutoff,))
        return cur.rowcount


# ---------- спостереження за лотами ("зниклі" = ймовірно продані) ----------

def update_listing_observations(watch_id, items):
    """
    Оновлює спостереження після ринкового сканування (N найновіших лотів,
    sort=newlyListed). Лот, створений ПІЗНІШЕ за найстаріший лот поточної
    видачі, мав би в ній бути; якщо його немає GONE_MISS_THRESHOLD разів
    поспіль — він зник (ймовірно куплений). Лоти, старші за вікно
    видачі, не оцінюємо: вони могли просто вийти за межі N найновіших.
    """
    now = int(time.time())
    present = {it["item_id"] for it in items if it.get("item_id")}
    created = [it["created_at"] for it in items if it.get("created_at")]
    window_start = min(created) if created else None

    with get_conn() as conn:
        for it in items:
            if not it.get("item_id"):
                continue
            conn.execute(
                """INSERT INTO listing_obs (watch_id, item_id, cond_group, spec_group, price,
                                            created_at, end_at, first_seen, last_seen, miss_count, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'active')
                   ON CONFLICT(watch_id, item_id) DO UPDATE SET
                     cond_group=excluded.cond_group, spec_group=excluded.spec_group,
                     price=excluded.price, end_at=excluded.end_at, last_seen=excluded.last_seen,
                     miss_count=0, status='active', gone_at=NULL""",
                (watch_id, it["item_id"], it["cond_group"], it.get("spec_group", "unspecified"),
                 it["total_price"], it.get("created_at"), it.get("end_at"), now, now),
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
            """SELECT cond_group, spec_group, price FROM listing_obs
               WHERE watch_id = ? AND status = 'active' AND last_seen >= ? AND price IS NOT NULL""",
            (watch_id, since),
        ).fetchall()]


def cleanup_old_listing_obs():
    cutoff = int(time.time()) - LISTING_OBS_RETENTION_DAYS * 86400
    with get_conn() as conn:
        return conn.execute("DELETE FROM listing_obs WHERE last_seen < ?", (cutoff,)).rowcount


# ---------- category_hints ----------

def add_category_hint(keywords, pct, reason):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO category_hints (keywords, pct, reason, created_at) VALUES (?, ?, ?, ?)",
            (keywords, pct, reason, int(time.time())),
        )
        return cur.lastrowid


def list_category_hints():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM category_hints ORDER BY id").fetchall()]


def remove_category_hint(hint_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM category_hints WHERE id = ?", (hint_id,))


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


# ============================================================
# EBAY КЛІЄНТ (Browse API)
# ============================================================

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
NETWORK_MAX_ATTEMPTS = 4
NETWORK_BACKOFF_SECONDS = 1.0
NETWORK_MAX_BACKOFF_SECONDS = 30.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_token_cache = {"token": None, "expires_at": 0}
_token_lock = threading.Lock()


def _retry_after_seconds(response):
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, min(float(value), NETWORK_MAX_BACKOFF_SECONDS))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(
                0.0,
                min(
                    (retry_at - datetime.now(timezone.utc)).total_seconds(),
                    NETWORK_MAX_BACKOFF_SECONDS,
                ),
            )
        except (TypeError, ValueError, OverflowError):
            return None


def _request_with_retries(method, url, **kwargs):
    """Виконує HTTP-запит з обмеженим retry для тимчасових помилок."""
    for attempt in range(1, NETWORK_MAX_ATTEMPTS + 1):
        try:
            response = requests.request(method, url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt == NETWORK_MAX_ATTEMPTS:
                log.error(
                    "Мережевий запит %s %s завершився після %s спроб: %s",
                    method, url, attempt, exc,
                )
                raise
            delay = min(
                NETWORK_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                NETWORK_MAX_BACKOFF_SECONDS,
            )
            log.warning(
                "Тимчасова мережева помилка %s %s (спроба %s/%s): %s; повтор через %.1fs",
                method, url, attempt, NETWORK_MAX_ATTEMPTS, exc, delay,
            )
            time.sleep(delay)
            continue

        if response.status_code not in RETRYABLE_STATUS_CODES:
            return response

        if attempt == NETWORK_MAX_ATTEMPTS:
            log.error(
                "HTTP %s для %s %s після %s спроб",
                response.status_code, method, url, attempt,
            )
            return response

        delay = _retry_after_seconds(response)
        if delay is None:
            delay = min(
                NETWORK_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                NETWORK_MAX_BACKOFF_SECONDS,
            )
        log.warning(
            "Тимчасова HTTP-помилка %s для %s %s (спроба %s/%s); повтор через %.1fs",
            response.status_code, method, url, attempt, NETWORK_MAX_ATTEMPTS, delay,
        )
        time.sleep(delay)

    raise RuntimeError("HTTP retry loop завершився без відповіді")


def _get_access_token():
    """
    _get_access_token() (і search_active_items(), який його викликає)
    запускається через asyncio.to_thread — тобто в окремому ОС-потоці.
    Фонова перевірка (scheduler) і, наприклад, команда /addwatch від
    користувача можуть викликати це ОДНОЧАСНО з різних потоків. Без
    блокування обидва могли б одночасно побачити прострочений кеш і
    зробити два зайві запити OAuth-токена одночасно — блокування
    прибирає цю гонку.
    """
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    with _token_lock:
        # Поки чекали на lock, інший потік міг уже оновити токен
        now = time.time()
        if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
            return _token_cache["token"]

        creds = f"{config.EBAY_CLIENT_ID}:{config.EBAY_CLIENT_SECRET}"
        b64_creds = base64.b64encode(creds.encode()).decode()

        resp = _request_with_retries(
            "POST",
            OAUTH_URL,
            headers={
                "Authorization": f"Basic {b64_creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["token"] = data["access_token"]
        # Час дії рахуємо від моменту ОТРИМАННЯ відповіді, а не від початку
        # запиту — інакше кешований токен вважатиметься дійсним трохи довше,
        # ніж насправді дозволяє eBay (на час мережевої затримки запиту).
        _token_cache["expires_at"] = time.time() + data["expires_in"]
        return _token_cache["token"]


def _browse_search(query, condition_ids="", exclude_terms="", limit=50,
                   category_id=None, min_price=None, fieldgroups=None, fresh=False,
                   sort="newlyListed", offset=0):
    """
    Спільний низькорівневий запит до item_summary/search.

    Категорія передається окремим параметром category_ids — у Browse API
    немає поля categoryIds всередині filter, тож такий фільтр eBay просто
    ігнорує. ID категорій різні на кожному маркетплейсі, тому вони
    беруться з відповіді самого ebay.de (get_category_options), а не
    хардкодяться.

    Мінімальна ціна — filter price:[X..] разом з priceCurrency:EUR
    (без валюти eBay фільтр ціни не застосовує).
    """
    token = _get_access_token()

    q = query
    for term in _search_tokens(exclude_terms):
        q += f" -{term}"

    params = {"q": q, "limit": str(min(max(limit, 1), 100)), "sort": sort}
    if offset:
        params["offset"] = str(offset)
    if category_id:
        params["category_ids"] = str(category_id)
    if fieldgroups:
        params["fieldgroups"] = fieldgroups

    filters_list = []
    if condition_ids:
        # eBay Browse API вимагає pipe ('|') як роздільник для
        # багатозначних фільтрів на кшталт conditionIds — у БД значення
        # зберігаються через кому (зручніше для CONDITION_PRESETS),
        # тому конвертуємо тут.
        ids_piped = condition_ids.replace(",", "|")
        filters_list.append(f"conditionIds:{{{ids_piped}}}")
    filters_list.append("buyingOptions:{FIXED_PRICE}")
    filters_list.append(f"itemLocationRegion:{ITEM_LOCATION_REGION}")
    filters_list.append(f"deliveryCountry:{DELIVERY_COUNTRY}")
    if min_price and float(min_price) > 0:
        filters_list.append(f"price:[{float(min_price):.2f}..]")
        filters_list.append("priceCurrency:EUR")
    params["filter"] = ",".join(filters_list)

    headers = {
        "Authorization": "Bearer " + token,
        "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
        "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country={DELIVERY_COUNTRY},zip={EBAY_BUYER_POSTAL_CODE}",
        "Cache-Control": "no-cache, no-store",
        "Pragma": "no-cache",
        "Content-Type": "application/json",
    }
    if fresh:
        headers["X-EBAY-C-REQUEST-ID"] = str(uuid.uuid4())

    resp = _request_with_retries("GET", SEARCH_URL, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_category_options(query, condition_ids=DEFAULT_CONDITION_IDS):
    """
    Питає в eBay, в яких категоріях ebay.de є результати за цим запитом
    (fieldgroups=CATEGORY_REFINEMENTS → refinement.categoryDistributions).
    Повертає список {id, name, count}, найбільші категорії першими.
    """
    data = _browse_search(
        query, condition_ids=condition_ids, limit=1, fieldgroups="CATEGORY_REFINEMENTS",
    )
    refinement = data.get("refinement") or {}
    options = []
    for dist in refinement.get("categoryDistributions") or []:
        cat_id = dist.get("categoryId")
        if not cat_id:
            continue
        options.append({
            "id": str(cat_id),
            "name": dist.get("categoryName") or f"Категорія {cat_id}",
            "count": int(dist.get("matchCount") or 0),
        })
    options.sort(key=lambda o: o["count"], reverse=True)
    return options[:MAX_CATEGORY_OPTIONS]


def search_active_items(query, condition_ids="", exclude_terms="", limit=50, fresh=False,
                        category_id=None, min_price=None, sort="newlyListed", offset=0):
    """
    Пошук активних оголошень через eBay Browse API.
    Сортування — за датою публікації (newlyListed), а не за ціною.
    Аукціони виключаються фільтром buyingOptions:{FIXED_PRICE} і
    додатково клієнтською перевіркою. Лоти з опцією BEST_OFFER
    позначаються окремим прапорцем.

    Регіон: лот мусить бути в межах ЄС (itemLocationRegion:EUROPEAN_UNION),
    і мусить бути можливість доставки саме в Німеччину (deliveryCountry).
    Категорія й мінімальна ціна — див. _browse_search().
    """
    data = _browse_search(
        query, condition_ids=condition_ids, exclude_terms=exclude_terms, limit=limit,
        category_id=category_id, min_price=min_price, fresh=fresh, sort=sort, offset=offset,
    )

    items = []
    for it in data.get("itemSummaries", []):
        title = it.get("title") or ""
        if not _title_matches_search(title, query, exclude_terms):
            continue

        buying_options = it.get("buyingOptions") or []
        if "FIXED_PRICE" not in buying_options:
            continue
        has_best_offer = "BEST_OFFER" in buying_options

        cond_group = condition_group_from_item(it)
        if cond_group == "parts":
            continue  # "на запчастини" — не той товар, що ми перепродаємо

        price_info = it.get("price", {})
        try:
            price = float(price_info.get("value"))
        except (TypeError, ValueError):
            continue

        item_currency = price_info.get("currency", "EUR").upper()
        exchange_rate = CURRENCY_TO_EUR.get(item_currency)
        if exchange_rate is None:
            log.debug("Пропущено лот %s: невідома валюта %s", it.get("itemId"), item_currency)
            continue

        shipping_cost = 0.0
        shipping_options = it.get("shippingOptions") or []
        if shipping_options:
            costs = []
            for opt in shipping_options:
                cost_info = opt.get("shippingCost", {})
                try:
                    costs.append(float(cost_info.get("value", 0)))
                except (TypeError, ValueError):
                    pass
            if costs:
                shipping_cost = min(costs)

        seller = it.get("seller") or {}
        feedback_score = seller.get("feedbackScore")
        feedback_pct = seller.get("feedbackPercentage")
        try:
            feedback_pct = float(feedback_pct) if feedback_pct is not None else None
        except (TypeError, ValueError):
            feedback_pct = None

        suspicious = False
        if feedback_score is not None and feedback_score < MIN_SELLER_FEEDBACK_SCORE:
            suspicious = True
        if feedback_pct is not None and feedback_pct < MIN_SELLER_FEEDBACK_PCT:
            suspicious = True

        total_price = (price + shipping_cost) * exchange_rate
        effective_price = (
            total_price * (1 - BEST_OFFER_ASSUMED_DISCOUNT_PCT / 100)
            if has_best_offer else total_price
        )

        items.append(
            {
                "item_id": it.get("itemId"),
                "title": title,
                "price": price,
                "shipping_cost": shipping_cost,
                "total_price": total_price,
                "effective_price": effective_price,
                "has_best_offer": has_best_offer,
                "currency": "EUR",
                "url": it.get("itemWebUrl"),
                "condition": it.get("condition"),
                "cond_group": cond_group,
                "created_at": _parse_ebay_ts(it.get("itemCreationDate")),
                "end_at": _parse_ebay_ts(it.get("itemEndDate")),
                "seller_feedback_score": feedback_score,
                "seller_feedback_pct": feedback_pct,
                "suspicious": suspicious,
            }
        )
    return items


def _parse_ebay_ts(value):
    """ISO-дата eBay ("2026-09-20T10:00:00.000Z") → unix timestamp або None."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def _watch_search_kwargs(w):
    """Параметри пошуку, збережені для конкретного відстеження."""
    return {
        "query": w["query"],
        "condition_ids": w["condition_ids"],
        "exclude_terms": w["exclude"],
        "category_id": w.get("category_id") or None,
        "min_price": w.get("min_price") or None,
    }


# ============================================================
# АНАЛІЗ ЦІН
# ============================================================

def compute_median(prices, min_sample_size=MIN_SAMPLE_SIZE):
    if len(prices) < min_sample_size:
        return None
    return statistics.median(prices)


def minimum_sample_size_for_query(query):
    """Мінімум оголошень у групі для надійної медіани. Менші групи не
    рахуються окремо — їхні лоти порівнюються із загальною групою стану."""
    return MIN_SAMPLE_SIZE


def percentile(values, pct):
    """Лінійна інтерполяція між сусідніми значеннями відсортованого списку."""
    ordered = sorted(values)
    if not ordered:
        return None
    k = (len(ordered) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def target_profit(sale_price, target_pct):
    """Бажаний прибуток: target_pct% від ціни продажу, але не менше MIN_PROFIT_EUR."""
    return max(sale_price * target_pct / 100, MIN_PROFIT_EUR)


def max_buy_price(sale_price, target_pct):
    """
    Найвища ціна купівлі (з доставкою), за якої перепродаж ще дає бажаний
    прибуток: ціна продажу − комісія eBay − доставка покупцю − прибуток.
    """
    net_sale = sale_price * (1 - EBAY_SELLING_FEES_PCT / 100) - RESALE_SHIPPING_EUR
    return net_sale - target_profit(sale_price, target_pct)


def estimate_resale_profit(sale_price, purchase_price):
    """
    Прибуток при перепродажу за sale_price після комісії eBay і доставки.
    ВАЖЛИВО: sale_price — це ОЦІНКА (ціни зниклих лотів або нижня частина
    активних пропозицій), а не підтверджена ціна продажу: eBay Browse API
    не показує завершені продажі. Повертає (sale_price, profit).
    """
    net_sale = sale_price * (1 - EBAY_SELLING_FEES_PCT / 100) - RESALE_SHIPPING_EUR
    return sale_price, net_sale - purchase_price


def filter_outliers(prices):
    if len(prices) < MIN_SAMPLE_SIZE:
        return prices
    sorted_p = sorted(prices)
    q1 = statistics.median(sorted_p[: len(sorted_p) // 2])
    q3 = statistics.median(sorted_p[(len(sorted_p) + 1) // 2:])
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    return [p for p in prices if lower <= p <= upper]


def suggest_min_price(query, category_id=None, condition_ids=DEFAULT_CONDITION_IDS):
    """
    Рахує медіану в обраній категорії і пропонує мінімальну ціну як
    MIN_PRICE_SUGGESTION_PCT% від неї (округлено до 5€). Повертає
    (min_price, median, sample_size) або None, якщо даних замало.
    """
    try:
        items = search_active_items(
            query=query, condition_ids=condition_ids, limit=100, category_id=category_id,
        )
    except Exception as e:
        log.warning("Не вдалося оцінити мінімальну ціну для «%s»: %s", query, e)
        return None
    prices = filter_outliers([it["total_price"] for it in items])
    if len(prices) < MIN_MODEL_SAMPLE_SIZE:
        return None
    median_price = statistics.median(prices)
    suggestion = max(5, round(median_price * MIN_PRICE_SUGGESTION_PCT / 100 / 5) * 5)
    return suggestion, median_price, len(prices)


def analyze_market_for_threshold(query: str, category_id=None, min_price=None):
    """
    Реальний аналіз ринку: робить запит до eBay Browse API за назвою
    товару (в обраній категорії та від мінімальної ціни), бере поточні
    активні оголошення і рахує коефіцієнт варіації цін (розкид
    відносно медіани). Що більший розкид — то вищий поріг знижки треба
    ставити, інакше цілком нормальна ціна для варіативного товару буде
    помилково сприйматись як "вигідна".

    Повертає dict {pct, reason, median, sample_size} на основі
    РЕАЛЬНИХ поточних даних, або None, якщо eBay недоступний чи лотів
    замало для надійного розрахунку.
    """
    try:
        items = search_active_items(
            query=query, limit=100, category_id=category_id, min_price=min_price,
        )
    except Exception as e:
        log.warning("Не вдалося проаналізувати ринок для «%s»: %s", query, e)
        return None

    if not items:
        return None

    prices = [it["total_price"] for it in items]
    clean = filter_outliers(prices)
    if len(clean) < MIN_SAMPLE_SIZE:
        return None

    median_price = statistics.median(clean)
    stdev = statistics.pstdev(clean)
    cv = (stdev / median_price) if median_price else 0  # коефіцієнт варіації

    if cv < 0.15:
        pct, desc = 15, "дуже стабільний ринок, ціни майже однакові"
    elif cv < 0.25:
        pct, desc = 20, "стандартизований товар, невеликий розкид цін"
    elif cv < 0.35:
        pct, desc = 25, "помірний розкид цін"
    elif cv < 0.50:
        pct, desc = 32, "великий розкид цін"
    else:
        pct, desc = 40, "дуже великий розкид цін, товар неоднорідний"

    pct = max(MIN_ALLOWED_THRESHOLD_PCT, min(MAX_ALLOWED_THRESHOLD_PCT, pct))
    reason = (
        f"на основі {len(clean)} поточних оголошень з eBay: медіана "
        f"{median_price:.0f}€, розкид цін ~{cv * 100:.0f}% ({desc})"
    )

    spec_warning = _detect_spec_price_mismatch(items)
    return {
        "pct": pct, "reason": reason, "median": median_price,
        "sample_size": len(clean), "spec_warning": spec_warning,
    }


def _detect_spec_price_mismatch(items):
    """
    Перевіряє, чи в межах одного запиту трапляються різні виявлені
    конфігурації (обсяг пам'яті/накопичувача) з помітно різними
    медіанними цінами — типовий приклад: "iPhone 12" без зазначення
    пам'яті змішує 64/128/256GB. Якщо так — повертає текст-підказку
    вказати конкретну конфігурацію в назві для точнішого відстеження.
    """
    by_spec = {}
    for it in items:
        spec = extract_spec_key(it["title"])
        if spec == "unspecified":
            continue
        by_spec.setdefault(spec, []).append(it["total_price"])

    candidates = {spec: statistics.median(prices) for spec, prices in by_spec.items() if len(prices) >= 3}
    if len(candidates) < 2:
        return None

    lowest_spec = min(candidates, key=candidates.get)
    highest_spec = max(candidates, key=candidates.get)
    low, high = candidates[lowest_spec], candidates[highest_spec]
    if low <= 0 or (high - low) / low < 0.15:
        return None  # різниця незначна, окреме уточнення не критичне

    parts = ", ".join(f"{spec} ~{price:.0f}€" for spec, price in sorted(candidates.items(), key=lambda kv: kv[1]))
    return (
        f"у результатах трапляються різні конфігурації з різними цінами ({parts}). "
        f"Бот і так рахує медіану окремо для кожної виявленої конфігурації, але для "
        f"точнішого відстеження краще вказати конкретний обсяг у назві (напр. «256GB»)."
    )


# ============================================================
# КОНТРОЛЬ ДОСТУПУ
# ============================================================

def is_owner(user_id: int) -> bool:
    return config.OWNER_TELEGRAM_ID != 0 and user_id == config.OWNER_TELEGRAM_ID


async def _notify_owner_new_request(bot, user):
    if config.OWNER_TELEGRAM_ID == 0:
        return
    name = user.username and f"@{user.username}" or user.first_name or str(user.id)
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Дозволити", callback_data=f"access:approve:{user.id}"),
            InlineKeyboardButton("⛔ Відхилити", callback_data=f"access:deny:{user.id}"),
        ]]
    )
    await bot.send_message(
        chat_id=config.OWNER_TELEGRAM_ID,
        text=f"🔔 Новий запит на доступ до бота\n👤 {name} (ID: {user.id})",
        reply_markup=keyboard,
    )
    await refresh_owner_menu(bot)


async def _ack_callback(update: Update):
    """Гасить 'годинник очікування' на inline-кнопці, якщо оновлення —
    натискання кнопки. Безпечно ігнорує помилку, якщо запит уже
    відповіли раніше (наприклад, повторний виклик у ланцюжку)."""
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass


def require_access(handler_func):
    """
    Декоратор для команд/колбеків, що доступні лише власнику або
    схваленим користувачам. Власник — повний доступ автоматично.
    Новий користувач — запит іде власнику з кнопками схвалення.
    """
    @functools.wraps(handler_func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _ack_callback(update)
        user = update.effective_user
        if is_owner(user.id):
            return await handler_func(update, context)

        row = get_user_row(user.id)
        status = row["status"] if row else None

        if status == "approved":
            return await handler_func(update, context)

        if status == "pending":
            await update.effective_message.reply_text(
                "⏳ Твій запит на доступ ще розглядається власником бота."
            )
            return

        if status == "denied":
            await update.effective_message.reply_text(
                "⛔ Власник бота вже відхилив твій попередній запит на доступ."
            )
            return

        # немає запису взагалі — це справді новий запит
        upsert_user_request(
            user.id, update.effective_chat.id, user.username, user.first_name
        )
        await _notify_owner_new_request(context.bot, user)
        await update.effective_message.reply_text(
            "🔒 Доступ до цього бота обмежений.\n"
            "Твій запит надіслано власнику — очікуй підтвердження."
        )
    return wrapper


def owner_only(handler_func):
    @functools.wraps(handler_func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _ack_callback(update)
        if not is_owner(update.effective_user.id):
            await update.effective_message.reply_text("⛔ Ця команда доступна лише власнику бота.")
            return
        return await handler_func(update, context)
    return wrapper


async def access_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    if not is_owner(update.effective_user.id):
        await query_cb.answer("⛔ Лише власник може приймати це рішення.", show_alert=True)
        return
    await query_cb.answer()

    _, action, user_id_str = query_cb.data.split(":")
    target_id = int(user_id_str)
    row = get_user_row(target_id)
    if not row:
        await query_cb.edit_message_text("Запит уже неактуальний.")
        return

    if action == "approve":
        set_user_status(target_id, "approved")
        await query_cb.edit_message_text(f"{query_cb.message.text}\n\n✅ Доступ надано.")
        try:
            await context.bot.send_message(
                chat_id=row["chat_id"],
                text="✅ Твій доступ до бота підтверджено! Напиши /start, щоб почати.",
            )
        except Exception as e:
            log.warning("Не вдалося сповістити користувача %s: %s", target_id, e)
    else:
        set_user_status(target_id, "denied")
        await query_cb.edit_message_text(f"{query_cb.message.text}\n\n⛔ Доступ відхилено.")
        try:
            await context.bot.send_message(
                chat_id=row["chat_id"],
                text="⛔ Власник бота відхилив твій запит на доступ.",
            )
        except Exception as e:
            log.warning("Не вдалося сповістити користувача %s: %s", target_id, e)

    await refresh_owner_menu(context.bot)


# ============================================================
# ДІАЛОГ ДОДАВАННЯ ВІДСТЕЖЕННЯ (/addwatch)
# Кроки: назва → категорія eBay → мінімальна ціна → поріг знижки
# ============================================================

(
    ASK_QUERY,
    ASK_CATEGORY,
    ASK_MIN_PRICE_CHOICE,
    ASK_CUSTOM_MIN_PRICE,
    ASK_THRESHOLD_CHOICE,
    ASK_CUSTOM_THRESHOLD,
) = range(6)

NEW_WATCH_KEYS = (
    "new_watch_query", "suggested_pct", "new_watch_category_id", "new_watch_category_name",
    "new_watch_category_options", "new_watch_min_price", "suggested_min_price",
)


def _clear_new_watch(context):
    for key in NEW_WATCH_KEYS:
        context.user_data.pop(key, None)


def _ebay_configured():
    return bool(config.EBAY_CLIENT_ID) and "PUT_YOUR" not in config.EBAY_CLIENT_ID


def _category_keyboard(options, prefix, extra_rows=None):
    """Кнопки вибору категорії: callback_data = f'{prefix}{index}' або f'{prefix}all'."""
    rows = []
    for idx, opt in enumerate(options):
        label = opt["name"] if len(opt["name"]) <= 40 else opt["name"][:37] + "…"
        rows.append([InlineKeyboardButton(f"🗂️ {label} ({opt['count']})", callback_data=f"{prefix}{idx}")])
    rows.append([InlineKeyboardButton("🌐 Усі категорії", callback_data=f"{prefix}all")])
    for row in extra_rows or []:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


@require_access
async def addwatch_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_new_watch(context)
    await show_panel(
        update, context,
        "📦 <b>Додавання товару</b>\n\n"
        "Введи назву/модель товару, який хочеш відстежувати на eBay.\n"
        "Приклад: iPhone 13 128GB, PlayStation 5 Pro, ThinkPad X1 Carbon Gen 10",
        reply_markup=cancel_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ASK_QUERY


async def addwatch_got_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    if not query:
        await show_panel(update, context, "Порожній запит не підходить. Спробуй ще раз.", reply_markup=cancel_keyboard())
        return ASK_QUERY

    chat_id = update.effective_chat.id
    duplicate = find_duplicate_watch(chat_id, query)
    if duplicate:
        await show_panel(
            update, context,
            f"⚠️ У тебе вже є відстеження #{duplicate['id']} «{html.escape(duplicate['query'])}» "
            f"з такою ж назвою (поріг {duplicate['discount_threshold_pct']}%).\n\n"
            f"Введи іншу назву — наприклад, додай конкретний обсяг пам'яті чи стан, "
            f"щоб відрізнити від наявного запису.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_QUERY

    context.user_data["new_watch_query"] = query

    if not _ebay_configured():
        return await _propose_threshold(update, context)

    await show_panel(
        update, context,
        f"🔎 «{html.escape(query)}»\n\nШукаю, в яких категоріях eBay є такі товари…",
        reply_markup=cancel_keyboard(),
    )
    try:
        options = await asyncio.to_thread(get_category_options, query)
    except Exception as e:
        log.warning("Не вдалося отримати категорії для «%s»: %s", query, e)
        options = []

    if not options:
        # Категорій не знайшлось — продовжуємо без обмеження категорією
        return await _propose_min_price(update, context)

    context.user_data["new_watch_category_options"] = options
    await show_panel(
        update, context,
        f"🗂️ <b>«{html.escape(query)}»: обери категорію</b>\n\n"
        "eBay знаходить цей запит у кількох категоріях. Обери ту, де сам товар "
        "(напр. консолі, а не ігри чи аксесуари) — бот шукатиме лише там.\n"
        "У дужках — кількість оголошень.",
        reply_markup=_category_keyboard(
            options, "cat:",
            extra_rows=[[InlineKeyboardButton("❌ Скасувати", callback_data="menu:home")]],
        ),
        parse_mode=ParseMode.HTML,
    )
    return ASK_CATEGORY


async def addwatch_category_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    await _ack_callback(update)
    choice = query_cb.data.split(":", 1)[1]
    options = context.user_data.get("new_watch_category_options") or []

    if choice != "all":
        try:
            opt = options[int(choice)]
        except (ValueError, IndexError):
            return ASK_CATEGORY
        context.user_data["new_watch_category_id"] = opt["id"]
        context.user_data["new_watch_category_name"] = opt["name"]
    return await _propose_min_price(update, context)


async def _propose_min_price(update, context):
    query = context.user_data["new_watch_query"]
    category_id = context.user_data.get("new_watch_category_id")
    category_name = context.user_data.get("new_watch_category_name")

    await show_panel(
        update, context,
        f"🔎 «{html.escape(query)}»\n\nАналізую ціни, щоб запропонувати мінімальну ціну…",
        reply_markup=cancel_keyboard(),
    )
    suggestion = await asyncio.to_thread(suggest_min_price, query, category_id)
    if suggestion is None:
        # Замало даних для пропозиції — без мінімальної ціни
        context.user_data["new_watch_min_price"] = 0
        return await _propose_threshold(update, context)

    min_price, median_price, sample_size = suggestion
    context.user_data["suggested_min_price"] = min_price
    cat_line = f"Категорія: {html.escape(category_name)}\n" if category_name else ""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ Від {min_price:.0f}€", callback_data="minp:use"),
            InlineKeyboardButton("✏️ Своя ціна", callback_data="minp:custom"),
        ],
        [InlineKeyboardButton("Без обмеження", callback_data="minp:none")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="menu:home")],
    ])
    await show_panel(
        update, context,
        f"💶 <b>«{html.escape(query)}»: мінімальна ціна</b>\n\n"
        f"{cat_line}"
        f"Медіана зараз: ~{median_price:.0f}€ (за {sample_size} оголошеннями).\n\n"
        f"Пропоную не враховувати лоти дешевші за <b>{min_price:.0f}€</b> "
        f"(~{MIN_PRICE_SUGGESTION_PCT}% від медіани) — так відсіюються ігри, "
        f"аксесуари й запчастини, які майже завжди значно дешевші за сам товар.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )
    return ASK_MIN_PRICE_CHOICE


async def addwatch_min_price_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    await _ack_callback(update)
    choice = query_cb.data.split(":", 1)[1]

    if choice == "custom":
        await show_panel(
            update, context,
            "✏️ Введи мінімальну ціну в євро (напр. 120), або 0 — без обмеження.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_CUSTOM_MIN_PRICE
    if choice == "use":
        context.user_data["new_watch_min_price"] = context.user_data.get("suggested_min_price", 0)
    else:
        context.user_data["new_watch_min_price"] = 0
    return await _propose_threshold(update, context)


async def addwatch_custom_min_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace("€", "").replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        await show_panel(update, context, "Це не схоже на число. Введи, наприклад: 120", reply_markup=cancel_keyboard())
        return ASK_CUSTOM_MIN_PRICE
    if value < 0:
        await show_panel(update, context, "Ціна не може бути відʼємною. Спробуй ще раз.", reply_markup=cancel_keyboard())
        return ASK_CUSTOM_MIN_PRICE
    context.user_data["new_watch_min_price"] = value
    return await _propose_threshold(update, context)


async def _propose_threshold(update, context):
    query = context.user_data["new_watch_query"]
    category_id = context.user_data.get("new_watch_category_id")
    min_price = context.user_data.get("new_watch_min_price") or None

    analysis = None
    validation_note = ""
    if _ebay_configured():
        await show_panel(
            update, context,
            f"🔎 «{html.escape(query)}»\n\nПеревіряю поточні ціни на eBay, зачекай кілька секунд…",
            reply_markup=cancel_keyboard(),
        )
        analysis = await asyncio.to_thread(analyze_market_for_threshold, query, category_id, min_price)
        if analysis is None:
            validation_note = (
                "\n\n⚠️ Не вдалося зібрати достатньо оголошень для аналізу ринку — "
                "поріг нижче запропоновано за загальною підказкою, а не за реальними цінами."
            )
    else:
        validation_note = "\n\n(аналіз ринку на eBay недоступний, поки не додані API-ключі — поріг за загальною підказкою)"

    if analysis:
        pct = analysis["pct"]
        reason = analysis["reason"]
        spec_warning = analysis.get("spec_warning")
        if spec_warning:
            validation_note += f"\n\n💡 {html.escape(spec_warning)}"
    else:
        pct, reason = find_threshold_suggestion(query)

    context.user_data["suggested_pct"] = pct

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"✅ Взяти {pct}%", callback_data="use_suggested"),
                InlineKeyboardButton("✏️ Своє значення", callback_data="use_custom"),
            ],
            [InlineKeyboardButton("❌ Скасувати", callback_data="menu:home")],
        ]
    )
    await show_panel(
        update, context,
        f"🔎 «{html.escape(query)}»\n\n"
        f"💡 Пропоную бажаний прибуток: {pct}% від ціни продажу (мінімум {MIN_PROFIT_EUR}€)\n"
        f"Причина: {html.escape(reason)}\n"
        f"Бот вважатиме лот вигідним, якщо після перепродажу (мінус комісія eBay і доставка) "
        f"лишається щонайменше цей прибуток"
        f"{validation_note}\n\n"
        f"Це лише рекомендація — остаточне рішення за тобою.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )
    return ASK_THRESHOLD_CHOICE


async def addwatch_threshold_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    await query_cb.answer()

    if query_cb.data == "use_suggested":
        pct = context.user_data["suggested_pct"]
        return await _finalize_watch(update, context, pct)

    await show_panel(
        update, context,
        f"✏️ Введи бажаний прибуток у відсотках від ціни продажу "
        f"(від {MIN_ALLOWED_THRESHOLD_PCT} до {MAX_ALLOWED_THRESHOLD_PCT}), напр: 30",
        reply_markup=cancel_keyboard(),
    )
    return ASK_CUSTOM_THRESHOLD


async def addwatch_custom_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace("%", "").replace(",", ".")
    try:
        pct = float(text)
    except ValueError:
        await show_panel(
            update, context, "Це не схоже на число. Введи, наприклад: 30",
            reply_markup=cancel_keyboard(),
        )
        return ASK_CUSTOM_THRESHOLD

    if not (MIN_ALLOWED_THRESHOLD_PCT <= pct <= MAX_ALLOWED_THRESHOLD_PCT):
        await show_panel(
            update, context,
            f"Значення має бути від {MIN_ALLOWED_THRESHOLD_PCT} до {MAX_ALLOWED_THRESHOLD_PCT}. Спробуй ще раз.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_CUSTOM_THRESHOLD

    return await _finalize_watch(update, context, pct)


async def _finalize_watch(update, context, pct):
    query = context.user_data["new_watch_query"]
    category_id = context.user_data.get("new_watch_category_id") or ""
    category_name = context.user_data.get("new_watch_category_name") or ""
    min_price = context.user_data.get("new_watch_min_price") or 0
    _clear_new_watch(context)
    chat_id = update.effective_chat.id

    wid = add_watch(
        chat_id=chat_id,
        label=query,
        query=query,
        exclude="broken defekt teile parts kaputt",
        condition_ids=DEFAULT_CONDITION_IDS,
        discount_threshold_pct=pct,
        category_id=category_id,
        category_name=category_name,
        min_price=min_price,
    )

    extras = []
    if category_name:
        extras.append(f"🗂️ Категорія: {html.escape(category_name)}")
    if min_price:
        extras.append(f"💶 Мінімальна ціна: {min_price:.0f}€")
    extras_txt = ("\n" + "\n".join(extras)) if extras else ""

    user_id = update.effective_user.id
    text = (
        f"✅ Додано відстеження #{wid}: «{html.escape(query)}», бажаний прибуток {pct}%."
        f"{extras_txt}\n\n{MAIN_MENU_TEXT}"
    )
    await show_panel(update, context, text, reply_markup=build_main_menu(user_id), parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def addwatch_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_new_watch(context)
    user_id = update.effective_user.id
    await show_panel(
        update, context,
        f"Скасовано.\n\n{MAIN_MENU_TEXT}",
        reply_markup=build_main_menu(user_id),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def addwatch_menu_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка меню посеред /addwatch — скасовує без збереження і
    відкриває натиснутий розділ у тій же панелі."""
    action = update.callback_query.data.split(":", 1)[1]
    _clear_new_watch(context)

    if action == "addwatch":
        return await addwatch_start(update, context)
    if action == "home":
        await show_main_menu(update, context)
    elif action == "list":
        await cmd_list(update, context)
    elif action == "stats":
        await cmd_stats(update, context)
    elif action == "categories":
        await cmd_categories(update, context)
    elif action == "pending":
        await cmd_pending(update, context)
    elif action == "users":
        await cmd_users(update, context)
    elif action == "addcategory":
        await _ack_callback(update)
        user_id = update.effective_user.id
        await show_panel(
            update, context,
            f"❌ Додавання товару скасовано (нічого не збережено).\n\n{MAIN_MENU_TEXT}",
            reply_markup=build_main_menu(user_id),
            parse_mode=ParseMode.HTML,
        )
    return ConversationHandler.END


# ============================================================
# КОМАНДИ КЕРУВАННЯ КАТЕГОРІЯМИ-ПІДКАЗКАМИ
# ============================================================

@require_access
async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hints = list_category_hints()
    if not hints:
        await show_panel(
            update, context, "Список категорій-підказок порожній.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    blocks = ["🏷️ <b>Категорії-підказки</b>"]
    for h in hints:
        keywords = html.escape(h["keywords"])
        reason = html.escape(h["reason"])
        blocks.append(
            f"<b>#{h['id']}</b>  {keywords}\n"
            f"    🎯 <b>{h['pct']:g}%</b> — {reason}"
        )

    rows = [
        [InlineKeyboardButton("🗑️ Видалити категорію", callback_data="delcat_prompt")],
        [InlineKeyboardButton("◀️ Меню", callback_data="menu:home")],
    ]
    await show_panel(
        update, context, "\n\n".join(blocks),
        reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
    )


ASK_DELETE_ID = 0  # окрема невеличка "розмова": лише один крок — увести номер


@require_access
async def delwatch_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["delete_target"] = "watch"
    watches = list_watches(chat_id=update.effective_chat.id)
    if not watches:
        context.user_data.pop("delete_target", None)
        await show_panel(
            update, context,
            "Немає активних відстежень для видалення.",
            reply_markup=back_to_menu_keyboard(),
        )
        return ConversationHandler.END
    watch_lines = [f"#{w['id']} {html.escape(w['label'])}" for w in watches]
    await show_panel(
        update, context,
        "🗑️ <b>Видалення відстеження</b>\n\n"
        "Активні відстеження:\n"
        + "\n".join(watch_lines)
        + "\n\nВведи номер (#) товару, який хочеш видалити.",
        reply_markup=cancel_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ASK_DELETE_ID


@require_access
async def delcat_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["delete_target"] = "category"
    hints = list_category_hints()
    if not hints:
        context.user_data.pop("delete_target", None)
        await show_panel(
            update, context,
            "Список категорій-підказок порожній.",
            reply_markup=back_to_menu_keyboard(),
        )
        return ConversationHandler.END
    category_lines = [f"#{h['id']} {html.escape(h['keywords'])}" for h in hints]
    await show_panel(
        update, context,
        "🗑️ <b>Видалення категорії</b>\n\n"
        "Категорії-підказки:\n"
        + "\n".join(category_lines)
        + "\n\nВведи номер (#) категорії, яку хочеш видалити.",
        reply_markup=cancel_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ASK_DELETE_ID


async def got_delete_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lstrip("#")
    try:
        item_id = int(raw)
    except ValueError:
        await show_panel(
            update, context, "Це не схоже на номер. Введи число, наприклад: 3",
            reply_markup=cancel_keyboard(),
        )
        return ASK_DELETE_ID

    target = context.user_data.get("delete_target")
    chat_id = update.effective_chat.id

    if target == "watch":
        watch = get_watch(item_id, chat_id)
        if not watch:
            await show_panel(
                update, context,
                f"Товару #{item_id} немає серед активних відстежень. Спробуй ще раз.",
                reply_markup=cancel_keyboard(),
            )
            return ASK_DELETE_ID
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Так, видалити", callback_data=f"delwatch_yes:{item_id}"),
                InlineKeyboardButton("❌ Ні", callback_data="menu:list"),
            ]]
        )
        await show_panel(
            update, context,
            f"Видалити відстеження #{item_id} «{html.escape(watch['label'])}»?",
            reply_markup=keyboard,
        )
    elif target == "category":
        hint = next((h for h in list_category_hints() if h["id"] == item_id), None)
        if not hint:
            await show_panel(
                update, context,
                f"Категорії #{item_id} немає в списку. Спробуй ще раз.",
                reply_markup=cancel_keyboard(),
            )
            return ASK_DELETE_ID
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Так, видалити", callback_data=f"delcat_yes:{item_id}"),
                InlineKeyboardButton("❌ Ні", callback_data="menu:categories"),
            ]]
        )
        await show_panel(
            update, context,
            f"Видалити категорію-підказку #{item_id} «{html.escape(hint['keywords'])}»?",
            reply_markup=keyboard,
        )
    else:
        await show_main_menu(update, context)

    context.user_data.pop("delete_target", None)
    return ConversationHandler.END


async def delete_menu_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка меню посеред введення номера для видалення — скасовує
    введення і одразу відкриває натиснутий розділ."""
    context.user_data.pop("delete_target", None)
    action = update.callback_query.data.split(":", 1)[1]
    if action == "list":
        await cmd_list(update, context)
    elif action == "stats":
        await cmd_stats(update, context)
    elif action == "categories":
        await cmd_categories(update, context)
    elif action == "pending":
        await cmd_pending(update, context)
    elif action == "users":
        await cmd_users(update, context)
    elif action == "addwatch":
        return await addwatch_start(update, context)
    elif action == "addcategory":
        return await addcategory_start(update, context)
    else:
        await show_main_menu(update, context)
    return ConversationHandler.END


async def delete_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("delete_target", None)
    await show_main_menu(update, context)
    return ConversationHandler.END


@require_access
async def delcat_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hid = int(update.callback_query.data.split(":")[1])
    remove_category_hint(hid)
    await _ack_callback(update)
    await cmd_categories(update, context)


# ============================================================
# ДІАЛОГ ДОДАВАННЯ КАТЕГОРІЇ-ПІДКАЗКИ (/addcategory)
# ============================================================

ASK_CAT_KEYWORDS, ASK_CAT_THRESHOLD, ASK_CAT_REASON = range(3)


@require_access
async def addcategory_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_panel(
        update, context,
        "🏷️ <b>Створюємо нову категорію-підказку</b>\n\n"
        "Крок 1/3. Введи ключові слова, за якими бот розпізнаватиме цей "
        "товар у назвах — через кому.\n"
        "Приклад: dyson, пилосос, vacuum cleaner",
        reply_markup=cancel_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ASK_CAT_KEYWORDS


async def addcategory_got_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keywords = update.message.text.strip()
    if not keywords:
        await show_panel(
            update, context, "Порожній список слів не підходить. Спробуй ще раз.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_CAT_KEYWORDS

    context.user_data["new_cat_keywords"] = keywords.lower()
    await show_panel(
        update, context,
        f"«{html.escape(keywords)}»\n\n"
        "Крок 2/3. Який поріг знижки (%) пропонувати для товарів цієї категорії?\n\n"
        "Орієнтир:\n"
        "• 15–20% — стандартизований товар, багато однакових лотів "
        "щодня (телефони, консолі)\n"
        "• 25–30% — товар з помірною варіативністю (ноутбуки, побутова техніка)\n"
        "• 35%+ — унікальний товар з великим розкидом цін (одяг, "
        "колекціонування, годинники)\n\n"
        f"Введи число від {MIN_ALLOWED_THRESHOLD_PCT} до {MAX_ALLOWED_THRESHOLD_PCT}, напр: 25",
        reply_markup=cancel_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ASK_CAT_THRESHOLD


async def addcategory_got_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace("%", "").replace(",", ".")
    try:
        pct = float(text)
    except ValueError:
        await show_panel(
            update, context, "Це не схоже на число. Введи, наприклад: 25",
            reply_markup=cancel_keyboard(),
        )
        return ASK_CAT_THRESHOLD

    if not (MIN_ALLOWED_THRESHOLD_PCT <= pct <= MAX_ALLOWED_THRESHOLD_PCT):
        await show_panel(
            update, context,
            f"Значення має бути від {MIN_ALLOWED_THRESHOLD_PCT} до {MAX_ALLOWED_THRESHOLD_PCT}. Спробуй ще раз.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_CAT_THRESHOLD

    context.user_data["new_cat_pct"] = pct
    await show_panel(
        update, context,
        "Крок 3/3. Коротко поясни, чому саме такий поріг для цієї "
        "категорії — це лише для твоєї власної довідки в /categories.\n\n"
        "Напиши текст, або /skip щоб залишити без пояснення.",
        reply_markup=cancel_keyboard(),
    )
    return ASK_CAT_REASON


async def addcategory_got_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text.strip()
    return await _finalize_category(update, context, reason)


async def addcategory_skip_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _finalize_category(update, context, "без опису")


async def _finalize_category(update, context, reason):
    keywords = context.user_data.pop("new_cat_keywords")
    pct = context.user_data.pop("new_cat_pct")

    hid = add_category_hint(keywords, pct, reason)
    user_id = update.effective_user.id
    text = (
        f"✅ Додано категорію-підказку #{hid}\n"
        f"🏷️ Ключові слова: {html.escape(keywords)}\n"
        f"🎯 Поріг: {pct}%\n"
        f"📝 Причина: {html.escape(reason)}\n\n"
        f"{MAIN_MENU_TEXT}"
    )
    await show_panel(update, context, text, reply_markup=build_main_menu(user_id), parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def addcategory_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_cat_keywords", None)
    context.user_data.pop("new_cat_pct", None)
    user_id = update.effective_user.id
    await show_panel(
        update, context,
        f"Скасовано.\n\n{MAIN_MENU_TEXT}",
        reply_markup=build_main_menu(user_id),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def addcategory_menu_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка меню посеред /addcategory — скасовує без збереження і
    відкриває натиснутий розділ у тій же панелі."""
    action = update.callback_query.data.split(":", 1)[1]
    context.user_data.pop("new_cat_keywords", None)
    context.user_data.pop("new_cat_pct", None)

    if action == "addcategory":
        return await addcategory_start(update, context)
    if action == "home":
        await show_main_menu(update, context)
    elif action == "list":
        await cmd_list(update, context)
    elif action == "stats":
        await cmd_stats(update, context)
    elif action == "categories":
        await cmd_categories(update, context)
    elif action == "pending":
        await cmd_pending(update, context)
    elif action == "users":
        await cmd_users(update, context)
    elif action == "addwatch":
        await _ack_callback(update)
        user_id = update.effective_user.id
        await show_panel(
            update, context,
            f"❌ Додавання категорії скасовано (нічого не збережено).\n\n{MAIN_MENU_TEXT}",
            reply_markup=build_main_menu(user_id),
            parse_mode=ParseMode.HTML,
        )
    return ConversationHandler.END


@require_access
async def cmd_removecategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Формат: /removecategory <id>")
        return
    try:
        hid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом, дивись /categories")
        return
    if not any(h["id"] == hid for h in list_category_hints()):
        await update.message.reply_text("Такої категорії немає, дивись /categories")
        return
    remove_category_hint(hid)
    await update.message.reply_text(f"🗑️ Категорію-підказку #{hid} видалено.")


# ============================================================
# ГОЛОВНЕ МЕНЮ + "ПАНЕЛЬ" (усе відбувається в одному повідомленні,
# яке редагується на кожному кроці, а не в потоці нових повідомлень)
# ============================================================

MENU_LABELS = {
    "addwatch": "➕ Додати товар",
    "addcategory": "🏷️ Додати категорію",
    "list": "📦 Мої відстеження",
    "stats": "📊 Статистика",
    "categories": "🗂️ Категорії",
    "pending": "⏳ Запити на доступ",
    "users": "👥 Користувачі",
}

MAIN_MENU_TEXT = "📋 <b>Головне меню</b> — обери дію:"


def build_main_menu(user_id: int) -> InlineKeyboardMarkup:
    """Inline-клавіатура, прикріплена до повідомлення в чаті. Рядок
    власника показується лише тоді, коли є що показувати."""
    def btn(action):
        return InlineKeyboardButton(MENU_LABELS[action], callback_data=f"menu:{action}")

    rows = [
        [btn("addwatch"), btn("addcategory")],
        [btn("list"), btn("stats")],
        [btn("categories")],
    ]
    if is_owner(user_id):
        owner_row = []
        if list_users(status="pending"):
            owner_row.append(btn("pending"))
        if list_users(status="approved"):
            owner_row.append(btn("users"))
        if owner_row:
            rows.append(owner_row)
    return InlineKeyboardMarkup(rows)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Меню", callback_data="menu:home")]])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="menu:home")]])


async def show_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                      reply_markup=None, parse_mode=None):
    """
    Показує вміст у ЄДИНОМУ "панельному" повідомленні для цього чату:
    редагує його на місці, або надсилає нове, якщо редагування
    неможливе. Повідомлення користувача (команди/введений текст)
    видаляються, щоб не висіли в історії чату.
    """
    chat_id = update.effective_chat.id
    if update.callback_query:
        context.user_data["panel_message_id"] = update.callback_query.message.message_id
    elif update.message:
        try:
            await update.message.delete()
        except Exception as e:
            log.debug("Не вдалося видалити повідомлення користувача: %s", e)

    panel_id = context.user_data.get("panel_message_id")
    if panel_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=panel_id, text=text,
                reply_markup=reply_markup, parse_mode=parse_mode,
            )
            return
        except Exception as e:
            log.debug("Не вдалося відредагувати панель (%s) — надсилаю нову", e)

    msg = await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode
    )
    context.user_data["panel_message_id"] = msg.message_id


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await show_panel(update, context, MAIN_MENU_TEXT, reply_markup=build_main_menu(user_id), parse_mode=ParseMode.HTML)


async def menu_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)


_owner_notice_state = {"message_id": None}


async def refresh_owner_menu(bot):
    """Оновлює ОДНЕ й те саме повідомлення з меню власника, а не плодить нові."""
    if config.OWNER_TELEGRAM_ID == 0:
        return
    kb = build_main_menu(config.OWNER_TELEGRAM_ID)
    msg_id = _owner_notice_state["message_id"]
    if msg_id:
        try:
            await bot.edit_message_text(
                chat_id=config.OWNER_TELEGRAM_ID, message_id=msg_id,
                text=MAIN_MENU_TEXT, reply_markup=kb, parse_mode=ParseMode.HTML,
            )
            return
        except Exception as e:
            log.debug("Не вдалося відредагувати меню власника (%s) — надсилаю нове", e)

    try:
        msg = await bot.send_message(
            chat_id=config.OWNER_TELEGRAM_ID,
            text=MAIN_MENU_TEXT,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        _owner_notice_state["message_id"] = msg.message_id
    except Exception as e:
        log.warning("Не вдалося оновити меню власника: %s", e)


# ============================================================
# ІНШІ TELEGRAM-КОМАНДИ
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_owner(user.id):
        row = get_user_row(user.id)
        status = row["status"] if row else None
        if status != "approved":
            if status == "pending":
                await update.message.reply_text("⏳ Твій запит на доступ ще розглядається власником бота.")
            else:
                upsert_user_request(user.id, update.effective_chat.id, user.username, user.first_name)
                await _notify_owner_new_request(context.bot, user)
                await update.message.reply_text(
                    "🔒 Доступ до цього бота обмежений.\n"
                    "Твій запит надіслано власнику — очікуй підтвердження."
                )
            return

    text = (
        "👋 Привіт! Я слідкую за цінами на eBay і сповіщаю, коли з'являється вигідний лот.\n\n"
        "Усе відбувається через кнопки меню нижче. Команди теж працюють:\n\n"
        "📦 Відстеження товарів\n"
        "/addwatch — додати новий товар (покроково)\n"
        "/list — активні відстеження\n"
        "/remove <id> — вимкнути відстеження\n"
        "/setthreshold <id> <%> — бажаний прибуток (% від ціни продажу)\n"
        "/setminprice <id> <€> — мінімальна ціна (0 — без обмеження)\n"
        "/setexclude <id> слова — виключені слова з пошуку\n"
        "/setconditions <id> <new|used|both> — які стани товару шукати\n"
        "\n"
        "🏷️ Категорії-підказки\n"
        "/categories — показати всі\n"
        "/addcategory — додати нову (покроково)\n"
        "/removecategory <id> — видалити\n\n"
        "📊 Статистика\n"
        "/stats — куплено/пропущено"
    )
    if is_owner(user.id):
        text += (
            "\n\n👑 Керування доступом (лише власник)\n"
            "/pending — запити, що очікують рішення\n"
            "/users — усі користувачі з доступом\n"
            "/userstats <id> — статистика конкретного користувача\n"
            "/revoke <id> — забрати доступ\n"
            "/approve <id> — дати доступ"
        )
    # ReplyKeyboardRemove прибирає стару системну клавіатуру знизу
    # екрана, яку Telegram міг закешувати від попередньої версії бота
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

    context.user_data.pop("panel_message_id", None)
    await show_main_menu(update, context)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показати головне меню знову (напр. якщо панель загубилась вище в чаті)."""
    context.user_data.pop("panel_message_id", None)
    await show_main_menu(update, context)


@require_access
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    watches = list_watches(chat_id=chat_id)
    if not watches:
        await show_panel(
            update, context, "Немає активних відстежень. Додай перше через «➕ Додати товар».",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    lines = ["📦 <b>Активні відстеження</b>\n", "Обери товар, щоб переглянути деталі:"]
    rows = []
    for w in watches:
        no_cat = "" if w.get("category_id") else " ⚠️ без категорії"
        lines.append(f"📌 <b>#{w['id']} {html.escape(w['label'])}</b>{no_cat}")
        rows.append(
            [
                InlineKeyboardButton(
                    f"📊 Відкрити #{w['id']}",
                    callback_data=f"watch_details:{w['id']}",
                )
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton("🗑️ Видалити товар", callback_data="delwatch_prompt")],
            [InlineKeyboardButton("◀️ Меню", callback_data="menu:home")],
        ]
    )
    await show_panel(
        update,
        context,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


def _watch_details_text(watch):
    stats = get_market_stats(watch["id"])
    category_txt = html.escape(watch.get("category_name") or "") or "усі (не обрана)"
    min_price = watch.get("min_price") or 0
    min_price_txt = f"{min_price:.0f}€" if min_price else "без обмеження"
    pct = watch["discount_threshold_pct"]
    lines = [
        f"📌 <b>#{watch['id']} {html.escape(watch['label'])}</b>",
        f"🎯 Бажаний прибуток: <b>{pct:g}%</b> від ціни продажу (мін. {MIN_PROFIT_EUR}€)",
        f"🗂️ Категорія: {category_txt}",
        f"💶 Мінімальна ціна: {min_price_txt}",
    ]
    if not stats:
        lines.append("\n💰 <b>Ринок:</b>\nЩе не проаналізований (потрібно ≥"
                     f"{MIN_SAMPLE_SIZE} оголошень у групі).")
        return "\n".join(lines)

    lines.append("\n💰 <b>Купівля і продаж:</b>")
    for s in sorted(stats, key=lambda s: (s["cond_group"], s["spec_group"] != "*", s["spec_group"])):
        sale_price = s["sale_price"] or s["median_price"]
        buy_limit = max_buy_price(sale_price, pct)
        _, profit = estimate_resale_profit(sale_price, buy_limit)
        trend = ""
        if s["prev_median_price"]:
            if s["median_price"] < s["prev_median_price"] * 0.98:
                trend = " ⬇️"
            elif s["median_price"] > s["prev_median_price"] * 1.02:
                trend = " ⬆️"
        lines.append(
            f"\n• <b>{html.escape(_group_label(s['cond_group'], s['spec_group']))}</b> "
            f"(оголошень: {s['sample_size']})\n"
            f"  🛒 Купувати до: <b>{buy_limit:.0f}€</b>\n"
            f"  💶 Продати за: ~<b>{sale_price:.0f}€</b> ({html.escape(s['sale_source'] or 'оцінка')})\n"
            f"  💰 Прибуток при цьому: ~{profit:.0f}€\n"
            f"  📊 Медіана пропозицій: {s['median_price']:.0f}€{trend}"
        )
    lines.append(
        "\n<i>Ціна продажу — оцінка: поки бот не назбирав ≥"
        f"{MIN_SOLD_SAMPLE} «зниклих» (ймовірно проданих) лотів, це нижня чверть "
        "поточних пропозицій. Прибуток — після комісії eBay і доставки.</i>"
    )
    return "\n".join(lines)


async def _show_watch_details(update, context, watch):
    watch_id = watch["id"]
    rows = [
        [InlineKeyboardButton("🔎 Переглянути оголошення", callback_data=f"view_listings:{watch_id}")],
        [InlineKeyboardButton("🧩 Усі конфігурації", callback_data=f"configs:{watch_id}")],
        [InlineKeyboardButton("🔄 Перерахувати медіану", callback_data=f"recalc_median:{watch_id}")],
        [InlineKeyboardButton("🗂️ Змінити категорію", callback_data=f"chcat:{watch_id}")],
        [InlineKeyboardButton("◀️ До активних відстежень", callback_data="menu:list")],
    ]
    await show_panel(
        update,
        context,
        _watch_details_text(watch),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


@require_access
async def watch_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
        return
    await _show_watch_details(update, context, watch)


@require_access
async def all_configs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Усі конфігурації з останнього ринкового сканування — зокрема ті, де
    оголошень замало для окремої оцінки (вони оцінюються через групу
    «усі конфігурації» відповідного стану)."""
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
        return

    back_rows = [[InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")]]
    listings = get_current_listings(watch_id)
    if not listings:
        await show_panel(
            update, context,
            f"🧩 <b>#{watch_id} {html.escape(watch['label'])}: конфігурації</b>\n\n"
            "Ще немає даних з ринкового сканування. Натисни «🔄 Перерахувати медіану» "
            "на екрані товару й відкрий цей список знову.",
            reply_markup=InlineKeyboardMarkup(back_rows),
            parse_mode=ParseMode.HTML,
        )
        return

    groups = {}
    for row in listings:
        groups.setdefault((row["cond_group"], row["spec_group"]), []).append(row["price"])

    lines = [
        f"🧩 <b>#{watch_id} {html.escape(watch['label'])}: усі конфігурації</b>",
        f"Оголошень в останньому скануванні: <b>{len(listings)}</b>",
    ]
    current_cond = None
    for (cond, spec), prices in sorted(groups.items(), key=lambda kv: (kv[0][0], statistics.median(kv[1]))):
        if cond != current_cond:
            current_cond = cond
            lines.append(f"\n<b>{html.escape(CONDITION_LABELS.get(cond, cond).capitalize())}</b>")
        spec_txt = "конфігурація не вказана" if spec == "unspecified" else spec
        enough = len(prices) >= MIN_SAMPLE_SIZE
        mark = "" if enough else " ⚠️"
        lines.append(
            f"• <b>{html.escape(spec_txt)}</b>{mark} — {len(prices)} огол., "
            f"від {min(prices):.0f}€, медіана {statistics.median(prices):.0f}€"
        )
    lines.append(
        f"\n<i>⚠️ — менше {MIN_SAMPLE_SIZE} оголошень: окремої оцінки «купити/продати» немає, "
        "лоти цієї конфігурації порівнюються із групою «усі конфігурації» свого стану.</i>"
    )
    await show_panel(
        update, context, "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(back_rows),
        parse_mode=ParseMode.HTML,
    )


@require_access
async def change_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує категорії eBay для вже доданого товару (кнопка «Змінити категорію»)."""
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    watch = get_watch(watch_id, update.effective_chat.id)
    if watch is None:
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
        return

    await show_panel(update, context, "🔎 Шукаю категорії eBay…")
    try:
        options = await asyncio.to_thread(get_category_options, watch["query"], watch["condition_ids"])
    except Exception as e:
        log.warning("Не вдалося отримати категорії для watch #%s: %s", watch_id, e)
        options = []

    context.user_data[f"cat_options_{watch_id}"] = options
    back_row = [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")]
    if not options:
        await show_panel(
            update, context,
            "Не вдалося отримати категорії від eBay. Спробуй пізніше.",
            reply_markup=InlineKeyboardMarkup([back_row]),
        )
        return
    await show_panel(
        update, context,
        f"🗂️ <b>#{watch_id} {html.escape(watch['label'])}: обери категорію</b>\n\n"
        "Після зміни медіану буде перераховано з нуля.",
        reply_markup=_category_keyboard(options, f"setcat:{watch_id}:", extra_rows=[back_row]),
        parse_mode=ParseMode.HTML,
    )


@require_access
async def set_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    _, watch_id_str, choice = query_cb.data.split(":", 2)
    watch_id = int(watch_id_str)
    chat_id = update.effective_chat.id
    watch = get_watch(watch_id, chat_id)
    if watch is None:
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
        return

    if choice == "all":
        update_watch_category(watch_id, chat_id, "", "")
    else:
        options = context.user_data.get(f"cat_options_{watch_id}") or []
        try:
            opt = options[int(choice)]
        except (ValueError, IndexError):
            await query_cb.answer("Список категорій застарів — відкрий його знову.", show_alert=True)
            return
        update_watch_category(watch_id, chat_id, opt["id"], opt["name"])
    context.user_data.pop(f"cat_options_{watch_id}", None)
    # Інша категорія — інша вибірка, стара статистика вже не відповідає
    reset_watch_market(watch_id)
    await _show_watch_details(update, context, get_watch(watch_id, chat_id))


@require_access
async def view_listings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    chat_id = update.effective_chat.id
    watch = get_watch(watch_id, chat_id)
    if watch is None:
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
        return

    try:
        # sort=price — eBay повертає лоти від найдешевших. Беремо із запасом
        # (частину відсіє перевірка назви) і показуємо 10 найдешевших.
        items = await asyncio.to_thread(
            search_active_items, limit=50, fresh=True, sort="price", **_watch_search_kwargs(watch),
        )
    except Exception as e:
        log.exception("Не вдалося завантажити оголошення для watch #%s: %s", watch_id, e)
        await query_cb.answer("Не вдалося завантажити оголошення. Спробуй ще раз.", show_alert=True)
        return

    items.sort(key=lambda item: item["total_price"])
    items = items[:10]
    nav_rows = [
        [InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{watch_id}")],
        [InlineKeyboardButton("📦 До відстежень", callback_data="menu:list")],
        [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
    ]

    if not items:
        await show_panel(
            update,
            context,
            f"🔎 <b>Оголошення для #{watch_id} {html.escape(watch['label'])}</b>\n\n"
            "Підходящих оголошень не знайдено.",
            reply_markup=InlineKeyboardMarkup(nav_rows),
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [
        f"🔎 <b>Оголошення для #{watch_id} {html.escape(watch['label'])}</b>",
        f"Найдешевші відповідні лоти: <b>{len(items)}</b>\n"
        f"🕒 Оновлено: {datetime.now().strftime('%H:%M:%S')}\n",
    ]
    rows = []
    for index, item in enumerate(items, 1):
        title = html.escape(item["title"][:160])
        lines.append(
            f"<b>{index}. {title}</b>\n"
            f"💶 {item['total_price']:.0f} {item['currency']} · "
            f"стан: {html.escape(item.get('condition') or 'н/д')}"
        )
        if item.get("url"):
            rows.append([InlineKeyboardButton(f"🔗 Відкрити оголошення #{index}", url=item["url"])])

    rows.extend(nav_rows)
    await show_panel(
        update,
        context,
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


@require_access
async def recalculate_median_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    watch_id = int(query_cb.data.split(":")[1])
    chat_id = update.effective_chat.id
    watch = get_watch(watch_id, chat_id)
    if watch is None:
        await query_cb.answer("Це відстеження вже не існує.", show_alert=True)
        return

    try:
        items, medians, _ = await _recalculate_watch_medians(watch, replace_existing=True)
    except Exception as e:
        log.exception("Не вдалося перерахувати медіану для watch #%s: %s", watch_id, e)
        await _notify_median_error(context.application, watch, e)
        await query_cb.answer("Не вдалося перерахувати медіану. Спробуй ще раз.", show_alert=True)
        return

    if not items:
        await _notify_median_problem(
            context.application,
            watch,
            "eBay не повернув жодного оголошення, яке відповідає фільтрам цього товару.",
        )
    elif not medians:
        minimum = minimum_sample_size_for_query(watch["query"])
        await _notify_median_problem(
            context.application,
            watch,
            f"Знайдено {len(items)} оголошень, але після фільтрації недостатньо "
            f"даних для медіани. Потрібно щонайменше {minimum} оголошення в одній "
            "групі стану та конфігурації.",
        )

    await _show_watch_details(update, context, get_watch(watch_id, chat_id))


@require_access
async def delwatch_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wid = int(update.callback_query.data.split(":")[1])
    chat_id = update.effective_chat.id
    remove_watch(wid, chat_id)
    await _ack_callback(update)
    await cmd_list(update, context)


@require_access
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Формат: /remove <id>")
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом, дивись /list")
        return
    if not get_watch(wid, chat_id):
        await update.message.reply_text("Такого відстеження немає, дивись /list")
        return
    remove_watch(wid, chat_id)
    await update.message.reply_text(f"🗑️ Відстеження #{wid} вимкнено.")


@require_access
async def cmd_setthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /setthreshold <id> <%>")
        return
    try:
        wid = int(context.args[0])
        pct = float(context.args[1].replace("%", ""))
    except ValueError:
        await update.message.reply_text("Некоректні значення. Формат: /setthreshold <id> <%>")
        return
    if not (MIN_ALLOWED_THRESHOLD_PCT <= pct <= MAX_ALLOWED_THRESHOLD_PCT):
        await update.message.reply_text(
            f"Значення має бути від {MIN_ALLOWED_THRESHOLD_PCT} до {MAX_ALLOWED_THRESHOLD_PCT}."
        )
        return
    if not get_watch(wid, chat_id):
        await update.message.reply_text("Такого відстеження немає, дивись /list")
        return
    update_threshold(wid, chat_id, pct)
    await update.message.reply_text(f"🎯 Бажаний прибуток для #{wid}: {pct}% від ціни продажу.")


@require_access
async def cmd_setminprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /setminprice <id> <€>\nПриклад: /setminprice 3 120 (0 — без обмеження)")
        return
    try:
        wid = int(context.args[0])
        value = float(context.args[1].replace("€", "").replace(",", "."))
    except ValueError:
        await update.message.reply_text("Некоректні значення. Формат: /setminprice <id> <€>")
        return
    if value < 0:
        await update.message.reply_text("Ціна не може бути відʼємною.")
        return
    if not get_watch(wid, chat_id):
        await update.message.reply_text("Такого відстеження немає, дивись /list")
        return
    update_watch_min_price(wid, chat_id, value)
    reset_watch_market(wid)  # інша вибірка — ринок аналізуємо заново
    shown = f"{value:.0f}€" if value else "без обмеження"
    await update.message.reply_text(f"💶 Мінімальна ціна для #{wid}: {shown}. Медіану буде перераховано.")


@require_access
async def cmd_setexclude(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) < 1:
        await update.message.reply_text(
            "Формат: /setexclude <id> слово1 слово2 ...\n"
            "Приклад: /setexclude 3 broken defekt teile parts kaputt\n"
            "Щоб очистити список — /setexclude <id> без слів."
        )
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом, дивись /list")
        return

    if not get_watch(wid, chat_id):
        await update.message.reply_text("Такого відстеження немає, дивись /list")
        return

    exclude_text = " ".join(context.args[1:])
    update_watch_exclude(wid, chat_id, exclude_text)
    reset_watch_market(wid)
    shown = exclude_text if exclude_text else "(РїРѕСЂРѕР¶РЅСЊРѕ)"
    await update.message.reply_text(f"🚫 Виключені слова для #{wid} оновлено: {shown}")


@require_access
async def cmd_setconditions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) != 2 or context.args[1].lower() not in CONDITION_PRESETS:
        await update.message.reply_text(
            "Формат: /setconditions <id> <new|used|both>\n"
            "new — лише новий товар, used — лише вживаний, "
            "both — обидва (за замовчуванням)."
        )
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом, дивись /list")
        return

    if not get_watch(wid, chat_id):
        await update.message.reply_text("Такого відстеження немає, дивись /list")
        return

    preset = context.args[1].lower()
    update_watch_conditions(wid, chat_id, CONDITION_PRESETS[preset])
    reset_watch_market(wid)
    await update.message.reply_text(f"🏷️ Стан товару для #{wid} змінено на «{preset}».")


@require_access
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    stats = get_deal_stats(chat_id)
    if not stats:
        await show_panel(update, context, "Поки що немає жодної знахідки.", reply_markup=back_to_menu_keyboard())
        return
    bought = stats.get("bought", 0)
    skipped = stats.get("skipped", 0)
    new = stats.get("new", 0)
    await show_panel(
        update, context,
        f"📊 Твоя статистика\n\n✅ Куплено: {bought}\n❌ Пропущено: {skipped}\n🆕 Ще не позначено: {new}",
        reply_markup=back_to_menu_keyboard(),
    )


@require_access
async def deal_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query

    action, deal_id_str = query_cb.data.split(":")
    deal_id = int(deal_id_str)

    owner_chat_id = get_deal_owner_chat_id(deal_id)
    if owner_chat_id != update.effective_chat.id:
        await query_cb.message.reply_text("⛔ Ця знахідка тобі не належить.")
        return

    status = "bought" if action == "buy" else "skipped"
    set_deal_status(deal_id, status)

    label = "✅ Куплено" if status == "bought" else "❌ Пропущено"
    await query_cb.edit_message_text(f"{query_cb.message.text}\n\n{label}")


# ============================================================
# КОМАНДИ ВЛАСНИКА: КЕРУВАННЯ ДОСТУПОМ І СТАТИСТИКА КОРИСТУВАЧІВ
# ============================================================

@owner_only
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = list_users(status="pending")
    if not pending:
        await show_panel(update, context, "Немає запитів, що очікують рішення.", reply_markup=back_to_menu_keyboard())
        return
    for u in pending:
        name = u["username"] and f"@{u['username']}" or u["first_name"] or str(u["user_id"])
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Дозволити", callback_data=f"access:approve:{u['user_id']}"),
                InlineKeyboardButton("⛔ Відхилити", callback_data=f"access:deny:{u['user_id']}"),
            ]]
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"👤 {name} (ID: {u['user_id']})",
            reply_markup=keyboard,
        )


@owner_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = list_users(status="approved")
    if not users:
        await show_panel(update, context, "Ще немає жодного схваленого користувача.", reply_markup=back_to_menu_keyboard())
        return
    lines = ["👥 Користувачі з доступом:\n"]
    for u in users:
        name = u["username"] and f"@{u['username']}" or u["first_name"] or str(u["user_id"])
        watch_count = len(list_watches(chat_id=u["chat_id"]))
        lines.append(f"👤 {name} — ID {u['user_id']} · 📦 {watch_count} відстежень")
    lines.append("\nДеталі по користувачу: /userstats <id>")
    await show_panel(update, context, "\n".join(lines), reply_markup=back_to_menu_keyboard())


@owner_only
async def cmd_userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Формат: /userstats <telegram_id>\nСписок id — /users")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом.")
        return

    row = get_user_row(target_id)
    if not row or row["status"] != "approved":
        await update.message.reply_text("Такого схваленого користувача немає.")
        return

    name = row["username"] and f"@{row['username']}" or row["first_name"] or str(target_id)
    watches = list_watches(chat_id=row["chat_id"])
    deal_stats = get_deal_stats(row["chat_id"])

    lines = [f"👤 {name} (ID: {target_id})\n"]
    if watches:
        lines.append("📦 Відстеження:")
        for w in watches:
            lines.append(f"  #{w['id']} {w['label']} — поріг {w['discount_threshold_pct']}%")
    else:
        lines.append("📦 Немає активних відстежень.")

    lines.append(
        f"\n📊 Знахідки: ✅ куплено {deal_stats.get('bought', 0)} · "
        f"❌ пропущено {deal_stats.get('skipped', 0)} · 🆕 нових {deal_stats.get('new', 0)}"
    )
    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Формат: /revoke <telegram_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом.")
        return
    set_user_status(target_id, "denied")
    await update.message.reply_text(f"⛔ Доступ для {target_id} забрано.")


@owner_only
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручне схвалення — зокрема, щоб передумати щодо раніше
    відхиленого (denied) користувача."""
    if not context.args:
        await update.message.reply_text("Формат: /approve <telegram_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом.")
        return
    row = get_user_row(target_id)
    if not row:
        await update.message.reply_text("Цей користувач ще не звертався до бота.")
        return
    set_user_status(target_id, "approved")
    await update.message.reply_text(f"✅ Доступ для {target_id} надано.")
    try:
        await context.bot.send_message(
            chat_id=row["chat_id"],
            text="✅ Твій доступ до бота підтверджено! Напиши /start, щоб почати.",
        )
    except Exception as e:
        log.warning("Не вдалося сповістити користувача %s: %s", target_id, e)


# ============================================================
# ФОНОВИЙ ЦИКЛ ПЕРЕВІРКИ
# ============================================================

async def check_all_watches(app: Application):
    watches = list_watches(active_only=True)
    for w in watches:
        try:
            await check_one_watch(app, w)
        except Exception as e:
            log.exception("Помилка при перевірці watch #%s: %s", w["id"], e)
            await _notify_median_error(app, w, e)
        await asyncio.sleep(1)


CONDITION_LABELS = {"new": "нові", "used": "вживані", "unknown": "стан невідомий"}


def _group_label(cond, spec):
    cond_txt = CONDITION_LABELS.get(cond, cond)
    spec_txt = "усі конфігурації" if spec == "*" else spec
    return f"{cond_txt}, {spec_txt}"


def _annotate_items(items):
    for it in items:
        it["spec_group"] = extract_spec_key(it["title"])
    return items


def _stat_for_item(stats, it):
    """Спершу статистика точної конфігурації; якщо окремої немає (замало
    оголошень) — загальна група цього стану."""
    return stats.get((it["cond_group"], it["spec_group"])) or stats.get((it["cond_group"], "*"))


def _fetch_market_items(w):
    """До MARKET_SCAN_PAGES × 100 найновіших оголошень — більша вибірка для ринку."""
    seen_ids, items = set(), []
    for page in range(MARKET_SCAN_PAGES):
        batch = search_active_items(limit=100, offset=page * 100, fresh=True, **_watch_search_kwargs(w))
        if not batch:
            break
        for it in batch:
            if it["item_id"] and it["item_id"] not in seen_ids:
                seen_ids.add(it["item_id"])
                items.append(it)
    return _annotate_items(items)


def _compute_group_stats(watch_id, items):
    """
    Групи статистики:
      (стан, "*")            — усі лоти цього стану; є завжди, якщо лотів ≥ MIN_SAMPLE_SIZE;
      (стан, конфігурація)   — окремо, лише якщо в конфігурації ≥ MIN_SAMPLE_SIZE лотів.
    Так та сама консоль не розсипається на дрібні групи з 3–4 оголошень.

    Для кожної групи:
      median_price — медіана цін пропозиції (для довідки й тренду);
      sale_price   — реалістична ціна продажу: медіана цін "зниклих" лотів,
                     якщо їх назбиралось ≥ MIN_SOLD_SAMPLE (не вище медіани
                     пропозицій — зняті продавцями дорогі лоти можуть її завищувати),
                     інакше SALE_PRICE_PERCENTILE-й перцентиль пропозицій.
    """
    groups = {}
    for it in items:
        groups.setdefault((it["cond_group"], "*"), []).append(it["total_price"])
        if it["spec_group"] != "unspecified":
            groups.setdefault((it["cond_group"], it["spec_group"]), []).append(it["total_price"])

    stats = {}
    for (cond, spec), prices in groups.items():
        # Конфігурація, що містить УСІ лоти цього стану, дублювала б групу "*"
        if spec != "*" and len(prices) == len(groups[(cond, "*")]):
            continue
        clean = filter_outliers(prices)
        if len(clean) < MIN_SAMPLE_SIZE:
            continue
        median_price = statistics.median(clean)
        gone = filter_outliers(get_gone_prices(watch_id, cond, None if spec == "*" else spec))
        if len(gone) >= MIN_SOLD_SAMPLE:
            sale_price = min(statistics.median(gone), median_price)
            source = f"зниклі лоти, {len(gone)} шт."
        else:
            sale_price = percentile(clean, SALE_PRICE_PERCENTILE)
            source = "нижня чверть пропозицій"
        stats[(cond, spec)] = {
            "cond_group": cond, "spec_group": spec,
            "median_price": median_price, "sale_price": sale_price,
            "sale_source": source, "sample_size": len(clean),
        }
    return stats


async def _recalculate_watch_medians(w: dict, replace_existing=False):
    """
    Ринкове сканування: велика вибірка найновіших лотів → спостереження
    за лотами (трекер зниклих) → статистика груп. Повертає
    (items, stats, newly_computed_keys).
    """
    items = await asyncio.to_thread(_fetch_market_items, w)
    if not items:
        return [], {}, []

    await asyncio.to_thread(update_listing_observations, w["id"], items)
    existing_keys = {(s["cond_group"], s["spec_group"]) for s in get_market_stats(w["id"])}
    stats = _compute_group_stats(w["id"], items)

    delete_market_stats_except(w["id"], set(stats))
    for (cond, spec), s in stats.items():
        upsert_market_stats(w["id"], cond, spec, s["median_price"], s["sample_size"],
                            s["sale_price"], s["sale_source"])
    newly = [key for key in stats if key not in existing_keys]
    return items, stats, newly


async def check_one_watch(app: Application, w: dict):
    rows = get_market_stats(w["id"])
    market_is_stale = (
        not rows
        or min(r["updated_at"] for r in rows) < time.time() - MARKET_REFRESH_MINUTES * 60
    )
    if market_is_stale:
        items, stats, newly = await _recalculate_watch_medians(w)
        if newly:
            await _notify_median_ready(app, w, [stats[key] for key in newly])
    else:
        stats = {(r["cond_group"], r["spec_group"]): r for r in rows}
        items = await asyncio.to_thread(
            search_active_items, limit=DEAL_SCAN_LIMIT, fresh=True, **_watch_search_kwargs(w),
        )
        _annotate_items(items)

    if not items or not stats:
        return

    new_deals = []
    for it in items:
        seen = get_seen_item(it["item_id"], w["id"])
        is_new = seen is None
        price_dropped = (not is_new) and seen["last_price"] is not None and it["effective_price"] < seen["last_price"] - 0.01

        if not is_new and not price_dropped:
            upsert_seen_item(it["item_id"], w["id"], it["effective_price"])
            continue

        stat = _stat_for_item(stats, it)
        if stat is None:
            upsert_seen_item(it["item_id"], w["id"], it["effective_price"])
            continue  # для цього стану ще немає надійної статистики

        sale_price = stat["sale_price"] or stat["median_price"]
        # Вигідно, якщо ціна купівлі (для Best Offer — з урахуванням торгу)
        # не вища за максимальну, що ще дає бажаний прибуток при перепродажі
        if it["effective_price"] > max_buy_price(sale_price, w["discount_threshold_pct"]):
            upsert_seen_item(it["item_id"], w["id"], it["effective_price"])
            continue
        discount_pct = (sale_price - it["total_price"]) / sale_price * 100

        already_notified_price = seen["last_notified_price"] if seen else None
        if already_notified_price is not None and it["effective_price"] >= already_notified_price - 0.01:
            upsert_seen_item(it["item_id"], w["id"], it["effective_price"])
            continue

        it["price_dropped"] = price_dropped and not is_new
        deal_id = add_deal(
            watch_id=w["id"],
            item_id=it["item_id"],
            title=it["title"],
            total_price=it["total_price"],
            currency=it["currency"],
            median_price=sale_price,
            discount_pct=discount_pct,
            url=it["url"],
            suspicious=it["suspicious"],
            has_best_offer=it["has_best_offer"],
        )
        upsert_seen_item(it["item_id"], w["id"], it["effective_price"], notified_price=it["effective_price"])
        new_deals.append((deal_id, it, stat))

    if not new_deals:
        return

    MAX_INDIVIDUAL_DEALS = 5
    if len(new_deals) <= MAX_INDIVIDUAL_DEALS:
        for deal_id, it, stat in new_deals:
            await _send_single_deal(app, w, deal_id, it, stat)
    else:
        await _send_grouped_deals(app, w, new_deals)


async def _send_single_deal(app, w, deal_id, it, stat):
    sale_price = stat["sale_price"] or stat["median_price"]
    _, profit = estimate_resale_profit(sale_price, it["total_price"])
    buy_limit = max_buy_price(sale_price, w["discount_threshold_pct"])

    warning = "\n⚠️ Низький рейтинг продавця — перевір уважно перед покупкою" if it["suspicious"] else ""
    best_offer_note = (
        "\n🎯 Можна запропонувати свою ціну (Best Offer) — реальна ціна може бути ще нижчою"
        if it["has_best_offer"] else ""
    )
    spec_note = f" · 💾 {it['spec_group']}" if it["spec_group"] != "unspecified" else ""
    price_drop_note = "\n🔻 Ціна впала ще нижче з моменту першої появи цього лота" if it.get("price_dropped") else ""

    text = (
        f"🔥 Вигідний лот: {w['label']}{price_drop_note}\n\n"
        f"{it['title']}\n"
        f"💶 Ціна з доставкою: {it['total_price']:.0f}€ (купувати варто до ~{buy_limit:.0f}€)\n"
        f"📈 Реалістична ціна продажу: ~{sale_price:.0f}€ ({stat['sale_source']})\n"
        f"📊 Медіана пропозицій: ~{stat['median_price']:.0f}€\n"
        f"💰 Орієнтовний прибуток: ~{profit:.0f}€ "
        f"(після комісії eBay ~{EBAY_SELLING_FEES_PCT}% і доставки ~{RESALE_SHIPPING_EUR}€)\n"
        f"🏷️ Стан: {it.get('condition') or 'н/д'}{spec_note}{warning}{best_offer_note}\n"
        f"⚠️ Ціна продажу — оцінка, а не підтверджений продаж.\n"
        f"🔗 {it['url']}"
    )
    buttons = [
        [
            InlineKeyboardButton("✅ Куплено", callback_data=f"buy:{deal_id}"),
            InlineKeyboardButton("❌ Пропущено", callback_data=f"skip:{deal_id}"),
        ],
        [InlineKeyboardButton("◀️ Меню", callback_data="menu:home")],
    ]
    await app.bot.send_message(chat_id=w["chat_id"], text=text, reply_markup=InlineKeyboardMarkup(buttons))


async def _send_grouped_deals(app, w, new_deals):
    lines = [f"🔥 Знайдено {len(new_deals)} вигідних лотів: {w['label']}\n"]
    for deal_id, it, stat in new_deals:
        sale_price = stat["sale_price"] or stat["median_price"]
        _, profit = estimate_resale_profit(sale_price, it["total_price"])
        warning = " ⚠️" if it["suspicious"] else ""
        offer_mark = " 🎯" if it["has_best_offer"] else ""
        drop_mark = " 🔻" if it.get("price_dropped") else ""
        spec_note = f" · 💾 {it['spec_group']}" if it["spec_group"] != "unspecified" else ""
        lines.append(
            f"💰 {it['total_price']:.0f}€ → продаж ~{sale_price:.0f}€, прибуток ~{profit:.0f}€"
            f"{spec_note}{warning}{offer_mark}{drop_mark}\n🔗 {it['url']}"
        )
    await app.bot.send_message(
        chat_id=w["chat_id"],
        text="\n\n".join(lines),
        reply_markup=back_to_menu_keyboard(),
    )


async def _notify_median_ready(app, w, new_stats):
    """Сповіщає, щойно для товару вперше порахувалась статистика групи."""
    lines = [f"📊 Ринок для «{w['label']}» проаналізовано — тепер шукаю вигідні лоти!\n"]
    for s in new_stats:
        sale_price = s["sale_price"] or s["median_price"]
        buy_limit = max_buy_price(sale_price, w["discount_threshold_pct"])
        lines.append(
            f"• {_group_label(s['cond_group'], s['spec_group'])}: продати ~{sale_price:.0f}€, "
            f"купувати до ~{buy_limit:.0f}€ ({s['sample_size']} оголошень)"
        )
    try:
        await app.bot.send_message(chat_id=w["chat_id"], text="\n".join(lines))
    except Exception as e:
        log.warning("Не вдалося надіслати сповіщення про ринок для watch #%s: %s", w["id"], e)


async def _notify_median_problem(app, w, reason):
    text = (
        f"⚠️ Не вдалося порахувати медіану для «{w['label']}».\n\n"
        f"Причина: {reason}\n\n"
        "Спробуй оновити оголошення пізніше або перевірити назву товару."
    )
    buttons = [[
        InlineKeyboardButton("📊 Відкрити товар", callback_data=f"watch_details:{w['id']}")
    ]]
    try:
        await app.bot.send_message(
            chat_id=w["chat_id"],
            text=text,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception as e:
        log.warning("Не вдалося надіслати пояснення проблеми медіани для watch #%s: %s", w["id"], e)


async def _notify_median_error(app, w, error):
    reason = str(error).strip() or error.__class__.__name__
    await _notify_median_problem(
        app,
        w,
        f"внутрішня помилка під час запиту або обробки даних: {reason}",
    )


async def scheduler_loop(app: Application):
    try:
        await asyncio.sleep(5)
        last_cleanup_at = 0
        while True:
            await check_all_watches(app)

            # Раз на добу прибираємо застарілі записи seen_items
            now = time.time()
            if now - last_cleanup_at > 86400:
                try:
                    removed = await asyncio.to_thread(cleanup_old_seen_items)
                    removed_obs = await asyncio.to_thread(cleanup_old_listing_obs)
                    if removed or removed_obs:
                        log.info("Очищено застарілих записів: seen_items %s, listing_obs %s",
                                 removed, removed_obs)
                except Exception as e:
                    log.warning("Не вдалося очистити seen_items: %s", e)
                last_cleanup_at = now

            await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)
    except asyncio.CancelledError:
        log.info("Фоновий планувальник зупинено")
        raise


async def post_init(app: Application):
    app.bot_data["scheduler_task"] = asyncio.create_task(scheduler_loop(app))
    # Список команд, що випадає над клавіатурою в Telegram
    await app.bot.set_my_commands([
        ("start", "Головне меню та довідка"),
        ("menu", "Показати меню"),
        ("addwatch", "➕ Додати товар для відстеження"),
        ("addcategory", "🏷️ Додати категорію-підказку"),
        ("list", "📦 Мої відстеження"),
        ("stats", "📊 Моя статистика"),
        ("categories", "🗂️ Список категорій"),
        ("remove", "Вимкнути відстеження за id"),
        ("setthreshold", "Змінити бажаний прибуток"),
        ("setminprice", "Змінити мінімальну ціну"),
        ("removecategory", "Видалити категорію за id"),
        ("cancel", "Скасувати поточну дію"),
    ])


async def post_shutdown(app: Application):
    task = app.bot_data.pop("scheduler_task", None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ============================================================
# ЗАПУСК
# ============================================================

def main():
    init_db()
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    menu_interrupt = CallbackQueryHandler(addwatch_menu_interrupt, pattern="^menu:")
    addwatch_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addwatch", addwatch_start),
            CallbackQueryHandler(addwatch_start, pattern="^menu:addwatch$"),
        ],
        states={
            ASK_QUERY: [
                menu_interrupt,
                MessageHandler(filters.TEXT & ~filters.COMMAND, addwatch_got_query),
            ],
            ASK_CATEGORY: [
                CallbackQueryHandler(addwatch_category_choice, pattern="^cat:"),
                menu_interrupt,
            ],
            ASK_MIN_PRICE_CHOICE: [
                CallbackQueryHandler(addwatch_min_price_choice, pattern="^minp:"),
                menu_interrupt,
            ],
            ASK_CUSTOM_MIN_PRICE: [
                menu_interrupt,
                MessageHandler(filters.TEXT & ~filters.COMMAND, addwatch_custom_min_price),
            ],
            ASK_THRESHOLD_CHOICE: [
                CallbackQueryHandler(addwatch_threshold_choice, pattern="^use_"),
                menu_interrupt,
            ],
            ASK_CUSTOM_THRESHOLD: [
                menu_interrupt,
                MessageHandler(filters.TEXT & ~filters.COMMAND, addwatch_custom_threshold),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", addwatch_cancel),
            menu_interrupt,
        ],
    )

    addcategory_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addcategory", addcategory_start),
            CallbackQueryHandler(addcategory_start, pattern="^menu:addcategory$"),
        ],
        states={
            ASK_CAT_KEYWORDS: [
                CallbackQueryHandler(addcategory_menu_interrupt, pattern="^menu:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcategory_got_keywords),
            ],
            ASK_CAT_THRESHOLD: [
                CallbackQueryHandler(addcategory_menu_interrupt, pattern="^menu:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcategory_got_threshold),
            ],
            ASK_CAT_REASON: [
                CallbackQueryHandler(addcategory_menu_interrupt, pattern="^menu:"),
                CommandHandler("skip", addcategory_skip_reason),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcategory_got_reason),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", addcategory_cancel),
            CallbackQueryHandler(addcategory_menu_interrupt, pattern="^menu:"),
        ],
    )

    delete_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(delwatch_prompt_callback, pattern="^delwatch_prompt$"),
            CallbackQueryHandler(delcat_prompt_callback, pattern="^delcat_prompt$"),
        ],
        states={
            ASK_DELETE_ID: [
                CallbackQueryHandler(delete_menu_interrupt, pattern="^menu:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_delete_id),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", delete_cancel),
            CallbackQueryHandler(delete_menu_interrupt, pattern="^menu:"),
        ],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(addwatch_conv)
    app.add_handler(addcategory_conv)
    app.add_handler(delete_conv)
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("setthreshold", cmd_setthreshold))
    app.add_handler(CommandHandler("setminprice", cmd_setminprice))
    app.add_handler(CommandHandler("setexclude", cmd_setexclude))
    app.add_handler(CommandHandler("setconditions", cmd_setconditions))
    app.add_handler(CommandHandler("categories", cmd_categories))
    app.add_handler(CommandHandler("removecategory", cmd_removecategory))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # Команди власника (керування доступом)
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("userstats", cmd_userstats))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("approve", cmd_approve))

    app.add_handler(CallbackQueryHandler(deal_action_callback, pattern="^(buy|skip):"))
    app.add_handler(CallbackQueryHandler(access_decision_callback, pattern="^access:"))
    # Кнопки головного меню (коли жоден діалог не активний)
    app.add_handler(CallbackQueryHandler(menu_home_callback, pattern="^menu:home$"))
    app.add_handler(CallbackQueryHandler(cmd_list, pattern="^menu:list$"))
    app.add_handler(CallbackQueryHandler(cmd_stats, pattern="^menu:stats$"))
    app.add_handler(CallbackQueryHandler(watch_details_callback, pattern="^watch_details:"))
    app.add_handler(CallbackQueryHandler(cmd_categories, pattern="^menu:categories$"))
    app.add_handler(CallbackQueryHandler(cmd_pending, pattern="^menu:pending$"))
    app.add_handler(CallbackQueryHandler(cmd_users, pattern="^menu:users$"))
    app.add_handler(CallbackQueryHandler(recalculate_median_callback, pattern="^recalc_median:"))
    app.add_handler(CallbackQueryHandler(view_listings_callback, pattern="^view_listings:"))
    app.add_handler(CallbackQueryHandler(change_category_callback, pattern="^chcat:"))
    app.add_handler(CallbackQueryHandler(all_configs_callback, pattern="^configs:"))
    app.add_handler(CallbackQueryHandler(set_category_callback, pattern="^setcat:"))
    # Підтвердження видалення
    app.add_handler(CallbackQueryHandler(delwatch_yes_callback, pattern="^delwatch_yes:"))
    app.add_handler(CallbackQueryHandler(delcat_yes_callback, pattern="^delcat_yes:"))

    log.info("Бот запущено")
    app.run_polling()


if __name__ == "__main__":
    main()