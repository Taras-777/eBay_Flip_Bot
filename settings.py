"""
Налаштування бота (не секретні): ліміти, пороги, регіон доставки, логування.
Секрети (токени й ключі) — у config.py.
"""

import config
import logging
import os
from datetime import timezone


try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Berlin")
except Exception:  # немає бази часових зон (напр. Windows без tzdata)
    LOCAL_TZ = timezone.utc


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


# Папка з даними бота (база + стан діалогів). У Docker це змонтований том /data,
# без Docker — поточна папка, як і раніше.
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)


DB_PATH = os.path.join(DATA_DIR, "ebay_flip_bot.sqlite3")


STATE_FILE = os.path.join(DATA_DIR, "bot_state.pickle")


# Лише значення за замовчуванням для старої колонки БД — у логіці не використовується
DEFAULT_DISCOUNT_THRESHOLD_PCT = 25


MIN_SAMPLE_SIZE = 8


MIN_MODEL_SAMPLE_SIZE = 3  # лише для пропозиції мінімальної ціни


CHECK_INTERVAL_MINUTES = 5   # пошук нових лотів — вигідні лоти розкуповують швидко


MIN_SELLER_FEEDBACK_SCORE = 5


MIN_SELLER_FEEDBACK_PCT = 95.0


BEST_OFFER_ASSUMED_DISCOUNT_PCT = 10


# Орієнтовна сукупна комісія eBay за продаж (final value fee) + оплата
# (PayPal/картка). Реальний відсоток залежить від категорії товару,
# це спрощена оцінка для прикидки прибутку — не точний розрахунок.
EBAY_SELLING_FEES_PCT = 15


# Орієнтовна вартість відправки товару покупцю при перепродажу (DHL/Hermes)
RESALE_SHIPPING_EUR = 7


# Єдиний критерій вигідності: лот вигідний, якщо після перепродажу
# (мінус комісія eBay і доставка) лишається щонайменше стільки євро.
# Хочеш суворіший відбір — збільш це число.
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


# Якщо в назві оголошення немає пам'яті/процесора, бот дізнається їх з
# характеристик лота (getItem → localizedAspects). Кожен лот запитується один
# раз (кеш у БД); ліміти захищають добову квоту eBay API.
ASPECT_LOOKUP_ENABLED = True


# Завантажувати характеристики для КОЖНОГО нового оголошення (а не лише для
# тих, де немає пам'яті в назві) — тоді фільтр "Kompatible Marke/Modell"
# працює для всіх лотів. Кожен лот перевіряється один раз (кеш).
ASPECT_LOOKUP_ALL = True


MAX_SPEC_LOOKUPS_PER_MARKET_SCAN = 150


MAX_SPEC_LOOKUPS_PER_DEAL_SCAN = 40


MAX_SPEC_LOOKUPS_PER_DAY = 3500


# Бюджет Browse API (пошук + характеристики). Ліміт eBay — 5000/добу;
# бот тримається нижче із запасом, щоб не отримати блокування (HTTP 429).
DAILY_BROWSE_BUDGET = 4500


SEARCH_RESERVE = 300          # стільки запитів завжди лишаємо для самого пошуку


LOW_BUDGET_INTERVAL_MINUTES = 30


CATEGORY_ASPECTS_CACHE_DAYS = 7


MAX_ASPECT_OPTIONS = 6


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


# Скільки днів тримати запис "цей лот уже бачили", перш ніж прибрати
# з таблиці seen_items. Без цього таблиця росте нескінченно — старі
# лоти давно зникли з eBay і повторно все одно ніколи не з'являться,
# тому їх ідентифікатори більше не потрібні.
SEEN_ITEMS_RETENTION_DAYS = 30


# Знахідки: куплені зберігаються завжди (статистика), решта — стільки днів
DEALS_RETENTION_DAYS = 180


API_USAGE_RETENTION_DAYS = 60


# ============================================================
# КОНТРОЛЬ ДОСТУПУ
# ============================================================

def is_owner(user_id: int) -> bool:
    return config.OWNER_TELEGRAM_ID != 0 and user_id == config.OWNER_TELEGRAM_ID


# ============================================================
# ФОНОВИЙ ЦИКЛ ПЕРЕВІРКИ
# ============================================================

WATCH_CONCURRENCY = 3  # скільки товарів перевіряти одночасно


ERROR_NOTICE_INTERVAL = 600  # не частіше ніж раз на 10 хв, щоб не спамити