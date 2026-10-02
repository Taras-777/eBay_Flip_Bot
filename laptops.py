"""
Ноутбуки: групування за класом замість точної конфігурації.

Ціну ноутбука визначають не SSD чи точна модель процесора, а:
  * Windows-ноутбук — відеокарта · покоління процесора · RAM
    («RTX 4060 · i7 13 gen · 16GB»);
  * MacBook — чип · екран · RAM · SSD («M3 Pro · 14" · 18GB · 512GB»);
    графіка в Mac вбудована в чип, RAM і SSD не замінити.

Частини класу йдуть від найважливішої, тож «батьківські» групи — це префікси:
«RTX 4060 · i7 13 gen · 16GB» → «RTX 4060 · i7 13 gen» → «RTX 4060» → усі.
Якщо в точному класі замало оголошень, ціна береться з найближчого ширшого.

Тут же — попередження, специфічні для ноутбуків (розкладка, блок живлення, BIOS…).
"""

import re

from textparse import SPEC_SIZE_PATTERN, _search_tokens, extract_cpu_token

SEP = " · "
UNKNOWN_GPU = "GPU ?"

# Категорії eBay, з яких оголошення вважається ноутбуком
LAPTOP_CATEGORY_WORDS = ("notebook", "laptop", "netbook")

# Лінійки ноутбуків — якщо вони є в запиті товару, це ноутбук навіть без категорії
LAPTOP_LINE_TERMS = {
    "laptop", "notebook", "macbook", "thinkpad", "latitude", "inspiron", "vostro", "precision", "xps",
    "ideapad", "zenbook", "vivobook", "chromebook", "elitebook", "probook", "yoga", "swift", "aspire",
    "victus", "omen", "legion", "loq", "nitro", "predator", "katana", "rog", "tuf", "strix", "zephyrus",
    "alienware", "aorus", "stealth", "raider", "pulse", "vector", "cyborg", "thinkbook",
}

# Бізнесові й тонкі лінійки: якщо відеокарта не згадана — майже завжди вбудована
IGPU_LINE_TERMS = {
    "thinkpad", "latitude", "elitebook", "probook", "zenbook", "vivobook", "yoga", "swift",
    "chromebook", "xps", "thinkbook", "inspiron", "vostro", "ideapad", "aspire",
}

GPU_ASPECTS = {"grafikprozessor", "gpu", "grafikkarte", "chipsatz/gpu-modell", "graphics processing type",
               "grafikverarbeitungstyp", "gpu-modell", "grafikchip"}
RAM_ASPECTS = {"arbeitsspeichergröße", "arbeitsspeicher", "ram size", "ram", "ram-größe"}
SSD_ASPECTS = {"ssd-speicherkapazität", "ssd capacity", "festplattenkapazität", "hard drive capacity",
               "speicherkapazität", "storage capacity"}
CPU_ASPECTS = {"prozessor", "processor", "prozessortyp", "processor type"}
SCREEN_ASPECTS = {"bildschirmgröße", "bildschirmdiagonale", "displaygröße", "screen size"}
LAYOUT_ASPECTS = {"tastaturlayout", "tastatursprache", "keyboard language", "keyboard layout"}

_GPU_PATTERNS = [
    (re.compile(r"\brtx\s*-?\s*a\s?(\d{3,4})\b", re.I), lambda m: f"RTX A{m.group(1)}"),
    (re.compile(r"\b(?:geforce\s*)?rtx\s*-?\s*(\d{4})\s*(ti)?\b", re.I),
     lambda m: f"RTX {m.group(1)}" + (" Ti" if m.group(2) else "")),
    (re.compile(r"\b(?:geforce\s*)?gtx\s*-?\s*(\d{3,4})\s*(ti)?\b", re.I),
     lambda m: f"GTX {m.group(1)}" + (" Ti" if m.group(2) else "")),
    (re.compile(r"\b(?:radeon\s*)?rx\s*-?\s*(\d{4})\s*(m|s|xt)?\b", re.I),
     lambda m: f"RX {m.group(1)}{(m.group(2) or '').upper()}"),
    (re.compile(r"\b(?:geforce\s*)?mx\s?(\d{3})\b", re.I), lambda m: f"MX {m.group(1)}"),
    (re.compile(r"\barc\s*a(\d{3})m?\b", re.I), lambda m: f"Arc A{m.group(1)}"),
]
_IGPU_PATTERN = re.compile(
    r"\b(iris\s*xe|iris|uhd\s*graphics|intel\s*graphics|radeon\s*graphics|radeon\s*vega|integriert\w*|on-?board|"
    r"integrated)\b", re.I)
# Відеопам'ять: «8GB GDDR6» / «8 GB VRAM» — завжди; число одразу після назви відеокарти
# («RTX 3060 6GB 16GB») — якщо це типовий обсяг відеопам'яті, а не RAM (див. _sizes)
_VRAM_TAGGED = re.compile(r"\b\d+\s?gb\s*(?:gddr\d\w*|vram)\b", re.I)
_VRAM_AFTER_GPU = re.compile(r"((?:rtx|gtx|rx|mx)\s*-?\s*a?\d{3,4}\s*(?:ti|m|s|xt)?\s*(?:mit\s*|with\s*)?)(\d+)\s?gb\b",
                             re.I)
_RAM_EXPLICIT = re.compile(r"\b(\d+)\s?gb\s*(?:ram|ddr\d\w*|lpddr\d\w*|arbeitsspeicher|unified|memory)\b", re.I)
_SCREEN_PATTERNS = [
    re.compile(r"\b(1[1-8])(?:[.,]\d)?\s*(?:\"|”|″|''|zoll|inch|-zoll|-inch|in\b)", re.I),
    re.compile(r"\bmacbook\s+(?:pro|air)\s+(1[3-6])\b", re.I),
]
_GEN_PATTERN = re.compile(r"\b(\d{1,2})\s*(?:th|nd|rd|st|\.)?\s*(?:gen\b|generation|generación)", re.I)


def is_laptop(title="", category_names=(), query=""):
    if any(w in (name or "").lower() for name in category_names or () for w in LAPTOP_CATEGORY_WORDS):
        return True
    tokens = _search_tokens(query) | _search_tokens(title)
    return bool(tokens & {"macbook"}) or bool(_search_tokens(query) & LAPTOP_LINE_TERMS)


_TRADEMARKS = re.compile(r"[™®©℠]")


def _plain(text):
    """«GeForce RTX™ 5050», «Intel® Core™ i5» → без знаків торгових марок (інакше шаблони не збігаються)."""
    return _TRADEMARKS.sub(" ", text or "")


def _aspect(aspects, names):
    for name, value in (aspects or {}).items():
        if name in names and str(value or "").strip():
            return _plain(str(value))
    return ""


def detect_gpu(text):
    text = _plain(text)
    for pattern, fmt in _GPU_PATTERNS:
        m = pattern.search(text or "")
        if m:
            return fmt(m)
    if _IGPU_PATTERN.search(text or ""):
        return "iGPU"
    return None


def _sizes(text):
    """(RAM ГБ або None, SSD ГБ або None) з тексту; відеопам'ять не рахується."""
    clean = _VRAM_TAGGED.sub(" ", text or "")
    m = _VRAM_AFTER_GPU.search(clean)
    if m:
        vram = int(m.group(2))
        others = [int(n) for n, u in SPEC_SIZE_PATTERN.findall(clean[:m.start(2)] + clean[m.end():])
                  if u.upper() == "GB" and 8 <= int(n) <= 64]
        # 2/4/6 ГБ — завжди відеопам'ять; 8/12/16 — лише якщо RAM указана окремо
        if vram <= 6 or (vram <= 16 and others):
            clean = clean[:m.start(2)] + " " + clean[m.end():]
    explicit = [int(n) for n in _RAM_EXPLICIT.findall(clean)]
    small, big = [], []
    for num, unit in SPEC_SIZE_PATTERN.findall(clean):
        gb = int(num) * (1024 if unit.upper() == "TB" else 1)
        (big if gb >= 128 else small).append(gb)
    ram = max(explicit) if explicit else (max(small) if small else None)
    return ram, (max(big) if big else None)


def _size_label(gb):
    return f"{gb // 1024}TB" if gb >= 1024 and gb % 1024 == 0 else f"{gb}GB"


def _ram_bucket(gb):
    if gb < 12:
        return "8GB"
    if gb <= 16:
        return "16GB"
    return "32GB+"


def _cpu_from(token, title, cpu_text=""):
    if not token:
        return None
    m = re.match(r"I([3579])(?:-(\d{3,5}))?", token)
    if m:
        tier, digits = m.group(1), m.group(2)
        if digits:
            gen = digits[:2] if len(digits) == 5 or (len(digits) == 4 and digits[0] == "1" and digits[1] in "01234") \
                else digits[0]
            return f"i{tier} {int(gen)} gen"
        g = _GEN_PATTERN.search(title) or _GEN_PATTERN.search(cpu_text)
        return f"i{tier} {int(g.group(1))} gen" if g else f"i{tier}"
    m = re.match(r"COREULTRA([3579])-(\d)", token)
    if m:
        return f"Core Ultra {m.group(1)} (S{m.group(2)})"
    m = re.match(r"RYZEN([3579])(?:-(\d)\d{3})?", token)
    if m:
        return f"Ryzen {m.group(1)} {m.group(2)}000" if m.group(2) else f"Ryzen {m.group(1)}"
    return None


def _cpu_class(title, cpu_text=""):
    """«i7 13 gen», «Ryzen 7 5000», «Core Ultra 7 (S1)», «i7» — або None. Якщо в назві лише «i5»,
    а в характеристиках «i5-12450H» — береться точніше з характеристик."""
    from_title = _cpu_from(extract_cpu_token(title), title, cpu_text)
    from_aspects = _cpu_from(extract_cpu_token(cpu_text), cpu_text)
    if from_title and from_aspects and from_aspects.startswith(from_title + " "):
        return from_aspects
    return from_title or from_aspects


def _screen(title, aspects):
    for text in (title, _aspect(aspects, SCREEN_ASPECTS)):
        for pattern in _SCREEN_PATTERNS:
            m = pattern.search(text or "")
            if m:
                return f'{m.group(1)}"'
    return None


def laptop_spec(title, aspects=None, query=""):
    """Клас ноутбука (див. опис модуля) або "unspecified", якщо з назви нічого не зрозуміло."""
    aspects = aspects or {}
    title = _plain(title)
    cpu_text = _aspect(aspects, CPU_ASPECTS)
    token = extract_cpu_token(title) or extract_cpu_token(cpu_text)
    ram, ssd = _sizes(title)
    if ram is None:
        ram = _sizes(_aspect(aspects, RAM_ASPECTS) + " RAM")[0]
    if ssd is None:
        ssd = _sizes(_aspect(aspects, SSD_ASPECTS))[1]

    # MacBook: чип · екран · RAM · SSD
    if token and re.fullmatch(r"M\d+(PRO|MAX|ULTRA)?", token):
        chip = re.sub(r"(PRO|MAX|ULTRA)$", lambda m: " " + m.group(1).capitalize(), token)
        parts = [chip, _screen(title, aspects)]
        parts += [_size_label(ram) if ram and ram <= 128 else None, _size_label(ssd) if ssd else None]
        return SEP.join(p for p in parts if p)

    # Windows-ноутбук: відеокарта · процесор · RAM
    gpu = detect_gpu(title) or detect_gpu(_aspect(aspects, GPU_ASPECTS))
    if gpu is None:
        lines = _search_tokens(query) | _search_tokens(title)
        gpu = "iGPU" if lines & IGPU_LINE_TERMS else UNKNOWN_GPU
    cpu = _cpu_class(title, cpu_text)
    if gpu == UNKNOWN_GPU and not cpu and not ram:
        return "unspecified"
    parts = [gpu, cpu, _ram_bucket(ram) if ram else None]
    return SEP.join(p for p in parts if p)


def needs_aspects(spec):
    """Ноутбук, про відеокарту якого з назви нічого не відомо, — варто глянути характеристики."""
    return spec == "unspecified" or spec.startswith(UNKNOWN_GPU)


def spec_parents(spec):
    """«A · B · C» → ["A · B", "A"] — ширші групи, від найближчої."""
    parts = (spec or "").split(SEP)
    return [SEP.join(parts[:i]) for i in range(len(parts) - 1, 0, -1)]


def spec_matches(item_spec, group_spec):
    """Чи входить оголошення з класом item_spec у групу group_spec (сама група, «*» або ширша)."""
    item_spec = item_spec or "unspecified"
    return group_spec == "*" or item_spec == group_spec or item_spec.startswith(group_spec + SEP)


# ---------- запчастини й аксесуари «для ноутбука» ----------

# «RAM passend für ASUS ROG Strix G15», «Netzteil für Lenovo Legion 5» — у назві модель
# ноутбука, але це не ноутбук. Справжній ноутбук майже завжди називає процесор чи відеокарту.
_FOR_PATTERN = re.compile(r"\b(für|fuer|passend|kompatibel|compatible|for|ersatz|replacement)\b", re.I)
_PART_PATTERN = re.compile(
    r"\b(so-?dimm|ddr[345]\w*|arbeitsspeicher|ram[\s-]?(modul|riegel|kit|upgrade)|speicher(modul|riegel)?|"
    r"netzteil|ladeger\w*|ladekabel|charger|akku|batterie|battery|tastatur|keyboard|display|bildschirm|"
    r"panel|lüfter|luefter|fan|kühler|scharnier\w*|hinge|gehäuse|cover|mainboard|motherboard|platine|"
    r"webcam|lautsprecher|kabel|adapter|dockingstation|tasche|hülle|skin|folie|schutzfolie)\b", re.I)
_DEVICE_WORD = re.compile(r"\b(laptop|notebook|macbook)\b", re.I)
PART_CATEGORY_WORDS = ("arbeitsspeicher", "speicher", "komponenten", "teile", "netzteil", "akku", "zubehör",
                       "lüfter", "kühl", "display", "tastatur", "mainboard", "gehäuse", "kabel", "adapter",
                       "ladegerät", "ersatzteil", "taschen")


def has_device_marker(title):
    """Процесор, відеокарта чи чип Apple у назві — ознака самого ноутбука, а не запчастини."""
    return bool(extract_cpu_token(title) or detect_gpu(title) not in (None, "iGPU"))


def looks_like_laptop_part(title, category_names=()):
    """Запчастина чи аксесуар для ноутбука, а не сам ноутбук: «für/passend/…» або слово
    запчастини (RAM, Netzteil, Akku…) чи категорія комплектуючих — і жодного процесора/відеокарти."""
    if has_device_marker(title):
        return False
    for_word = _FOR_PATTERN.search(title or "")
    part_word = _PART_PATTERN.search(title or "")
    if for_word and part_word:          # «Akku für Laptop», «RAM passend für ROG Strix»
        return True
    # Продавець сам поклав у категорію ноутбуків — це ноутбук, навіть якщо в назві лише
    # «Display 144Hz» чи «Tastatur beleuchtet» (відкидаємо лише явне «… für/passend …» вище)
    if any(w in (name or "").lower() for name in category_names or () for w in LAPTOP_CATEGORY_WORDS):
        return False
    # «Laptop … beleuchtete Tastatur» / «Notebook für Studenten» — сам ноутбук
    if (for_word or part_word) and not _DEVICE_WORD.search(title or ""):
        return True
    return any(w in (name or "").lower() for name in category_names or () for w in PART_CATEGORY_WORDS)


# ---------- ⚠️ попередження ----------

_WARNINGS = [
    (re.compile(r"\b(us|uk)[\s-]*(layout|tastatur\w*|keyboard|qwerty)|\bqwerty\b|englisch\w*\s+tastatur|"
                r"amerikanisch\w*\s+tastatur|\b(us|uk)[\s-]*(int|international)\b", re.I),
     "⚠️ Розкладка US/UK — у Німеччині продається дешевше"),
    (re.compile(r"\b(ohne|kein\w*|no)\s+(netzteil|ladeger\w*|ladekabel|charger|netzkabel)", re.I),
     "⚠️ Без блоку живлення"),
    (re.compile(r"bios[\s-]*(passwort|password|gesperrt|lock\w*|pw)|icloud|aktivierungssperre|\bmdm\b|"
                r"passwort\s+(vergessen|gesperrt)", re.I),
     "⚠️ Пароль BIOS / iCloud / MDM — може бути заблокований"),
    (re.compile(r"\b(ohne|kein\w*|no)\s+(ssd|festplatte|hdd|speicher|storage|m\.?2)\b", re.I),
     "⚠️ Без SSD"),
    (re.compile(r"akku\s*(defekt|schwach|tauschen|kaputt|hält\s+nicht|verbraucht)|batterie\s*defekt|"
                r"battery\s*(dead|bad|service)", re.I),
     "⚠️ Акумулятор слабкий чи несправний"),
    (re.compile(r"display\s*(schaden|defekt|gebrochen|riss\w*|kaputt)|displayschaden|pixelfehler|"
                r"bildschirm\s*(defekt|gebrochen|kaputt)|riss\s+im\s+(display|bildschirm)", re.I),
     "⚠️ Пошкоджений екран"),
]


def laptop_warnings(title, aspects=None):
    """Короткі попередження для картки вигідної пропозиції (лише попередження — не фільтр)."""
    found = [msg for pattern, msg in _WARNINGS if pattern.search(title or "")]
    layout = _aspect(aspects, LAYOUT_ASPECTS).lower()
    if layout and not re.search(r"deutsch|german|qwertz|\bde\b", layout) and _WARNINGS[0][1] not in found:
        found.insert(0, _WARNINGS[0][1])
    return found
