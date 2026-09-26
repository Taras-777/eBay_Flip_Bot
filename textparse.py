"""
Розбір тексту оголошень: стан товару, конфігурація (пам'ять, процесор),
перевірка назви на відповідність запиту, переклад назв категорій.
"""

import re


# Характеристики, наявність яких означає аксесуар чи запчастину ("для якого
# пристрою підходить") — однаково для будь-якого типу товару
COMPAT_ASPECTS = {
    "kompatible marke", "kompatibles modell", "kompatible produktlinie", "kompatibel mit",
    "compatible brand", "compatible model", "compatible product line",
}


# Загальні характеристики, які є і в товару, і в аксесуара — як "обов'язкова
# характеристика" нічого не доводять, тож не пропонуються
GENERIC_ASPECTS = {
    "marke", "brand", "modell", "model", "farbe", "color", "colour", "herstellernummer", "mpn",
    "ean", "upc", "isbn", "produktart", "type", "typ", "besonderheiten", "features",
    "herstellungsland und -region", "country/region of manufacture", "zustand", "condition",
    "material", "stil", "style", "gebrauchsanweisung", "herstellergarantie", "durability guarantee",
}


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
        or "generalüberholt" in c
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


# «PS5», «PS 5», «PlayStation 5», «Playstation5» — цифра 5 має стояти одразу
# після назви консолі. Окремі «playstation» і «5» у різних місцях назви
# («PlayStation 3 ... 3.5.0», «PlayStation 4 ... 5 Controllern») — не PS5.
PS5_TITLE_PATTERN = re.compile(
    r"(?<![\w.])(?:ps|play\s*-?\s*station)\s*-?\s*5(?![\d.,])", re.IGNORECASE
)


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
    "portal", "remote",  # PlayStation Portal / Remote Player — не консоль
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


PHONE_QUERY_TERMS = {"iphone", "galaxy", "pixel", "smartphone", "handy", "xiaomi", "oneplus"}


# Слова, характерні для оголошень аксесуарів/ремонту, а не самого телефона.
# "akku" свідомо немає: "Akku 90%" — звичайна частина назви вживаного телефона.
PHONE_ACCESSORY_TERMS = {
    "case", "cases", "cover", "hülle", "hulle", "handyhülle", "schutzhülle", "tasche",
    "bumper", "wallet", "strap", "band", "skin", "sticker", "folie", "schutzfolie",
    "panzerglas", "schutzglas", "displayschutz", "glass", "protector", "lens", "linse",
    "objektiv", "kabel", "cable", "ladegerät", "ladekabel", "charger", "adapter",
    "halterung", "holder", "reparatur", "repair", "ersatzteil", "ersatzteile",
    "backcover", "rückseite", "kamera", "camera", "dummy", "attrappe",
}


PHONE_FOR_WORDS = {"für", "fur", "for"}


# Категорії eBay з аксесуарами/запчастинами — при виборі позначаються ⚠️
ACCESSORY_CATEGORY_WORDS = (
    "zubehör", "zubehor", "accessor", "ersatzteil", "parts", "hüllen", "cases",
    "kabel", "taschen", "schutz", "ladegerät", "halterung",
)


# Переклад назв категорій ebay.de (eBay віддає їх лише німецькою). Спершу
# шукаємо всю назву; інакше перекладаємо по словах — і лише якщо відомі ВСІ
# слова. Неперекладні назви лишаються німецькими (краще, ніж хибний переклад).
CATEGORY_PHRASES_UK = {
    "handys & smartphones": "Мобільні телефони й смартфони",
    "handy-zubehör": "Аксесуари для телефонів",
    "handy-ersatzteile": "Запчастини для телефонів",
    "konsolen": "Консолі",
    "videospielkonsolen": "Ігрові консолі",
    "spielkonsolen": "Ігрові консолі",
    "konsolen-zubehör": "Аксесуари для консолей",
    "videospiele": "Відеоігри",
    "pc- & videospiele": "ПК- та відеоігри",
    "notebooks & netbooks": "Ноутбуки й нетбуки",
    "notebook-zubehör": "Аксесуари для ноутбуків",
    "notebook-ersatzteile": "Запчастини для ноутбуків",
    "computer, tablets & netzwerk": "Комп'ютери, планшети й мережа",
    "tablets & ebook-reader": "Планшети й електронні книги",
    "tablet-zubehör": "Аксесуари для планшетів",
    "pc-zubehör": "Аксесуари для ПК",
    "tv, video & audio": "ТБ, відео й аудіо",
    "kopfhörer": "Навушники",
    "foto & camcorder": "Фото й відеокамери",
    "digitalkameras": "Цифрові камери",
    "kamera-zubehör": "Аксесуари для камер",
    "objektive": "Об'єктиви",
    "objektive & filter": "Об'єктиви й фільтри",
    "uhren & schmuck": "Годинники й прикраси",
    "armbanduhren": "Наручні годинники",
    "smartwatches": "Смарт-годинники",
    "kleidung & accessoires": "Одяг і аксесуари",
    "herrenschuhe": "Чоловіче взуття",
    "damenschuhe": "Жіноче взуття",
    "sneaker": "Кросівки",
    "haushaltsgeräte": "Побутова техніка",
    "staubsauger": "Пилососи",
    "sonstige": "Інше",
}


CATEGORY_WORDS_UK = {
    "handys": "мобільні телефони", "smartphones": "смартфони", "handy": "телефони",
    "zubehör": "аксесуари", "ersatzteile": "запчастини", "konsolen": "консолі",
    "spiele": "ігри", "videospiele": "відеоігри", "notebooks": "ноутбуки", "netbooks": "нетбуки",
    "laptops": "ноутбуки", "tablets": "планшети", "computer": "комп'ютери", "netzwerk": "мережа",
    "kopfhörer": "навушники", "audio": "аудіо", "video": "відео", "tv": "ТБ", "uhren": "годинники",
    "schmuck": "прикраси", "schuhe": "взуття", "sneaker": "кросівки", "kameras": "камери",
    "objektive": "об'єктиви", "foto": "фото", "monitore": "монітори", "drucker": "принтери",
    "grafikkarten": "відеокарти", "festplatten": "жорсткі диски", "speicher": "пам'ять",
    "kabel": "кабелі", "adapter": "адаптери", "hüllen": "чохли", "taschen": "сумки",
    "akkus": "акумулятори", "ladegeräte": "зарядні пристрої", "controller": "контролери",
    "gamepads": "геймпади", "sonstige": "інше", "sonstiges": "інше", "weitere": "інші",
    "retro": "ретро", "pc": "ПК", "desktop": "настільні", "lautsprecher": "колонки",
    "smartwatches": "смарт-годинники", "fernseher": "телевізори", "für": "для", "&": "і",
    "apple": "Apple", "samsung": "Samsung", "sony": "Sony", "playstation": "PlayStation",
    "xbox": "Xbox", "nintendo": "Nintendo",
}


def translate_category(name):
    """Українська назва категорії або None, якщо перекласти не вдалось."""
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in CATEGORY_PHRASES_UK:
        return CATEGORY_PHRASES_UK[key]
    if key.endswith("-zubehör"):
        base = translate_category(name.strip()[: -len("-zubehör")])
        return f"Аксесуари ({base.lower()})" if base else None
    tokens = re.findall(r"[^\s,&\-]+|&", key)  # дефіс теж розділяє: "retro-konsolen"
    words = []
    for token in tokens:
        if token not in CATEGORY_WORDS_UK:
            return None
        words.append(CATEGORY_WORDS_UK[token])
    text = " ".join(words)
    return text[:1].upper() + text[1:] if text else None


def category_label(name, with_original=False):
    """Назва категорії для показу: українською, якщо є переклад
    (з німецьким оригіналом у дужках за потреби), інакше — оригінал."""
    uk = translate_category(name)
    if not uk:
        return name
    return f"{uk} ({name})" if with_original else uk


def is_accessory_category(name):
    lowered = (name or "").lower()
    return any(word in lowered for word in ACCESSORY_CATEGORY_WORDS)


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
        if not PS5_TITLE_PATTERN.search(title):
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

    if query_tokens.intersection(PHONE_QUERY_TERMS):
        # Чохол/скло/ремонт "für iPhone 13 Pro" — не телефон. Але лот
        # "iPhone 13 Pro 128GB + Hülle" з пам'яттю в назві — це телефон.
        has_phone_evidence = bool(SPEC_SIZE_PATTERN.search(title))
        looks_like_accessory = bool(title_tokens.intersection(PHONE_ACCESSORY_TERMS)) or (
            bool(title_tokens.intersection(PHONE_FOR_WORDS)) and not has_phone_evidence
        )
        if looks_like_accessory and not has_phone_evidence:
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


# Назви характеристик на ebay.de (і англійські варіанти), з яких беремо
# пам'ять, накопичувач і процесор
STORAGE_ASPECTS = {
    "speicherkapazität", "storage capacity", "ssd-speicherkapazität", "ssd capacity",
    "festplattenkapazität", "hard drive capacity", "kapazität", "capacity",
}


RAM_ASPECTS = {"arbeitsspeichergröße", "arbeitsspeicher", "ram size", "ram"}


LAPTOP_STORAGE_ASPECTS = {"ssd-speicherkapazität", "ssd capacity", "festplattenkapazität", "hard drive capacity"}


CPU_ASPECTS = {"prozessor", "processor", "prozessortyp", "processor type"}


def spec_key_from_aspects(title, aspects):
    """Конфігурація з назви, доповнена характеристиками лота (формат той
    самий, що й у extract_spec_key: напр. "16GB+512GB+M1PRO")."""
    tokens = {f"{num}{unit.upper()}" for num, unit in SPEC_SIZE_PATTERN.findall(title or "")}
    cpu = extract_cpu_token(title)
    # RAM беремо з характеристик лише в ноутбуків (є характеристика SSD/диска).
    # У телефонів "Arbeitsspeicher: 6 GB" розбило б одну модель на групи
    # "128GB" і "128GB+6GB" залежно від того, звідки взяли пам'ять.
    laptop_like = any(name in LAPTOP_STORAGE_ASPECTS for name in aspects)
    for name, value in aspects.items():
        if name in STORAGE_ASPECTS or (name in RAM_ASPECTS and laptop_like):
            tokens.update(f"{num}{unit.upper()}" for num, unit in SPEC_SIZE_PATTERN.findall(value or ""))
        elif name in CPU_ASPECTS and not cpu:
            cpu = extract_cpu_token(value)
    if cpu:
        tokens.add(cpu)
    return "+".join(sorted(tokens)) if tokens else "unspecified"


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


CONDITION_LABELS = {"new": "нові", "used": "вживані", "unknown": "стан невідомий"}


def _group_label(cond, spec):
    cond_txt = CONDITION_LABELS.get(cond, cond)
    spec_txt = "усі конфігурації" if spec == "*" else spec
    return f"{cond_txt}, {spec_txt}"


def _aspect_satisfied_by_title(required_aspect, title):
    """Пам'ять можна взяти з назви ("256GB") — тоді характеристики лота не потрібні."""
    name = (required_aspect or "").lower()
    return (name in STORAGE_ASPECTS or name in RAM_ASPECTS) and bool(SPEC_SIZE_PATTERN.search(title or ""))


def spec_required_by_default(query):
    """Для телефонів, ноутбуків і консолей оголошення без відомої пам'яті
    майже завжди — аксесуар, чохол чи запчастина."""
    tokens = _search_tokens(query)
    return bool(tokens & (PHONE_QUERY_TERMS | LAPTOP_QUERY_TERMS | CONSOLE_QUERY_TERMS))