"""
Ноутбуки: групування за класом замість точної конфігурації.

Ціну ноутбука визначають не SSD чи точна модель процесора, а:
  * Windows-ноутбук — відеокарта · покоління процесора · RAM
    («RTX 4060 · i7 13 gen · 16GB»);
  * MacBook — чип · екран · RAM («M3 Pro · 14" · 18GB»);
    графіка в Mac вбудована в чип, RAM не замінити. SSD не враховується.

Частини класу йдуть від найважливішої, тож «батьківські» групи — це префікси:
«RTX 4060 · i7 13 gen · 16GB» → «RTX 4060 · i7 13 gen» → «RTX 4060» → усі.
Якщо в точному класі замало оголошень, ціна береться з найближчого ширшого.

Тут же — попередження, специфічні для ноутбуків (розкладка, блок живлення, BIOS…).
"""

import html
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


# Опис оголошення: деякі продавці (часто магазини) пишуть відеокарту, процесор чи пам'ять лише
# в описі, а не в назві й не в характеристиках. Опис приходить у тій самій відповіді getItem,
# що й характеристики, — окремого запиту немає. Зберігаємо лише знайдені значення, не весь опис.
# Ці значення беруться, лише коли в назві й характеристиках нічого немає.
DESC_GPU_ASPECT = "grafikkarte (aus beschreibung)"
DESC_CPU_ASPECT = "prozessor (aus beschreibung)"
DESC_RAM_ASPECT = "arbeitsspeicher (aus beschreibung)"
DESC_SSD_ASPECT = "ssd (aus beschreibung)"
DESC_SCREEN_ASPECT = "bildschirm (aus beschreibung)"
DESC_CHECKED = "_beschreibung_geprueft"   # опис уже переглянуто (щоб не перечитувати)
# Версія розбору опису: коли бот навчився читати більше (напр. «RAM: 16 GB , DDR4»), оголошення
# з неповним класом, прочитані старішою версією, перечитуються один раз
DESC_VERSION = "2"
_HTML_BLOCKS = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_HTML_TAGS = re.compile(r"<[^>]+>")
# Процесори в описі. Apple — лише з «Apple» перед чипом або «Chip» після (інакше «M2 SSD» — це M.2)
_DESC_CPU_PATTERNS = [
    re.compile(r"\bApple\s+M\d{1,2}(?:\s?(?:Pro|Max|Ultra))?\b|\bM\d{1,2}(?:\s?(?:Pro|Max|Ultra))?(?=[\s-]*Chip)", re.I),
    re.compile(r"\bCore\s+Ultra\s+[3579]\s+\d{3}[A-Za-z]{0,2}\b", re.I),
    re.compile(r"\bi[3579][-\s]?\d{3,5}[A-Za-z]{0,2}\d{0,2}\b", re.I),
    re.compile(r"\bRyzen\s?[3579]\s?\d{3,4}[A-Za-z]{0,2}\b", re.I),
    re.compile(r"\bRyzen\s+AI\s+(?:Max\+?\s+)?[3579]\s+(?:HX\s+|PRO\s+)?\d{3}\b", re.I),
    re.compile(r"\bCore\s+[3579]\s+\d{3}[A-Za-z]{0,2}\b", re.I),
]
# «1 TB PCIe 4.0 NVMe M.2 SSD» або «SSD: 512 GB»; між об'ємом і «SSD» — без іншого об'єму
# (інакше «16 GB RAM, 512 GB SSD» дало б 16GB)
_DESC_SSD = re.compile(r"\b(\d{2,4}|[1-8])\s?(GB|TB)\b(?:(?!\d+\s?(?:GB|TB))[\s\w.,-]){0,25}?\bSSD\b"
                       r"|\bSSD\b\s*[:\-]?\s*(\d{2,4}|[1-8])\s?(GB|TB)\b", re.I)


# RAM в описі: «32 GB DDR5», «24 GB gemeinsamer Arbeitsspeicher», «16 GB Unified Memory»
_DESC_RAM = re.compile(
    r"\b(\d+)\s?gb\s*[,;:/-]?\s*(?:[a-zäöü]+\s+)?(?:ram|ddr\d\w*|lpddr\d\w*|arbeitsspeicher|unified|memory)\b"
    # «RAM: 16 GB», «Arbeitsspeicher: 32 GB»
    r"|\b(?:ram|arbeitsspeicher(?:größe)?|memory)\s*[:\-]?\s*(\d+)\s?gb\b", re.I)


def _description_text(description):
    text = _HTML_TAGS.sub(" ", _HTML_BLOCKS.sub(" ", description or ""))
    return _plain(html.unescape(text))


def _unique(values):
    values = set(values)
    return values.pop() if len(values) == 1 else None


def gpu_from_description(description):
    """Відеокарта з опису (HTML або текст). Якщо в описі кілька різних відеокарт (напр. «є з RTX 5060
    і RTX 5070») — невідомо, яка саме в цьому ноутбуці, тоді None."""
    if not description:
        return None
    text = _description_text(description)
    return _unique(fmt(m) for pattern, fmt in _GPU_PATTERNS for m in pattern.finditer(text))


def specs_from_description(description):
    """{псевдо-характеристика: значення} з опису: відеокарта, процесор, RAM, SSD, екран.
    Значення — лише якщо воно в описі одне (кілька різних — невідомо, яке з них)."""
    if not description:
        return {}
    text = _description_text(description)
    result = {}
    gpu = _unique(fmt(m) for pattern, fmt in _GPU_PATTERNS for m in pattern.finditer(text))
    if gpu:
        result[DESC_GPU_ASPECT] = gpu
    cpus = {}
    for pattern in _DESC_CPU_PATTERNS:
        for m in pattern.finditer(text):
            token = extract_cpu_token(m.group(0)) or extract_cpu_token(m.group(0) + " Chip")
            if token:
                cpus.setdefault(token, " ".join(m.group(0).split()))
    if len(cpus) == 1:
        result[DESC_CPU_ASPECT] = next(iter(cpus.values()))
    no_vram = _VRAM_TAGGED.sub(" ", text)
    ram = _unique(int(a or b) for a, b in _DESC_RAM.findall(no_vram) if 4 <= int(a or b) <= 128)
    if ram:
        result[DESC_RAM_ASPECT] = f"{ram} GB"
    ssd = _unique(int(m.group(1) or m.group(3)) * (1024 if (m.group(2) or m.group(4)).upper() == "TB" else 1)
                  for m in _DESC_SSD.finditer(text)
                  if int(m.group(1) or m.group(3)) * (1024 if (m.group(2) or m.group(4)).upper() == "TB" else 1) >= 64)
    if ssd:
        result[DESC_SSD_ASPECT] = _size_label(ssd)
    screen = _unique(m.group(1) for m in _SCREEN_PATTERNS[0].finditer(text))
    if screen:
        result[DESC_SCREEN_ASPECT] = f'{screen}"'
    return result


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
    m = re.match(r"RYZENAI([3579])-(\d)\d{2}", token)
    if m:
        return f"Ryzen AI {m.group(1)} {m.group(2)}00"
    m = re.match(r"CORE([3579])-(\d)\d{2}", token)
    if m:
        return f"Core {m.group(1)} (S{m.group(2)})"
    # Ryzen 200 (2025): «Ryzen 5 240» — тризначний номер; старші — чотиризначні («Ryzen 5 5600H»)
    m = re.match(r"RYZEN([3579])-(\d)\d{2}(?!\d)", token)
    if m:
        return f"Ryzen {m.group(1)} {m.group(2)}00"
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
    for text in (title, _aspect(aspects, SCREEN_ASPECTS), _aspect(aspects, {DESC_SCREEN_ASPECT})):
        for pattern in _SCREEN_PATTERNS:
            m = pattern.search(text or "")
            if m:
                return f'{m.group(1)}"'
    return None


def _best_cpu_token(*tokens):
    """Перший відомий процесор; якщо далі є точніший того ж чипа («M4» → «M4PRO») — точніший."""
    known = [t for t in tokens if t]
    if not known:
        return None
    best = known[0]
    for t in known[1:]:
        if t != best and t.startswith(best) and re.fullmatch(r"M\d+", best):
            return t
    return best


# SSD у Mac буває лише таких об'ємів; «500 GB» / «1000 GB» у назві — це 512GB / 1TB
APPLE_SSD_SIZES = (128, 256, 512, 1024, 2048, 4096, 8192)


def _apple_ssd(gb):
    nearest = min(APPLE_SSD_SIZES, key=lambda size: abs(size - gb))
    return nearest if abs(nearest - gb) <= nearest * 0.1 else gb


def laptop_spec(title, aspects=None, query=""):
    """Клас ноутбука (див. опис модуля) або "unspecified", якщо з назви нічого не зрозуміло.
    Порядок джерел: назва → характеристики → опис оголошення."""
    aspects = aspects or {}
    title = _plain(title)
    cpu_text = _aspect(aspects, CPU_ASPECTS) or _aspect(aspects, {DESC_CPU_ASPECT})
    token = _best_cpu_token(extract_cpu_token(title), extract_cpu_token(_aspect(aspects, CPU_ASPECTS)),
                            extract_cpu_token(_aspect(aspects, {DESC_CPU_ASPECT})))
    ram = _sizes(title)[0]
    if ram is None:
        ram = _sizes(_aspect(aspects, RAM_ASPECTS) + " RAM")[0]
    if ram is None:
        ram = _sizes(_aspect(aspects, {DESC_RAM_ASPECT}) + " RAM")[0]

    # MacBook: чип · екран · RAM
    if token and re.fullmatch(r"M\d+(PRO|MAX|ULTRA)?", token):
        chip = re.sub(r"(PRO|MAX|ULTRA)$", lambda m: " " + m.group(1).capitalize(), token)
        screen = _screen(title, aspects)
        plain_chip = re.fullmatch(r"M\d+", token) is not None
        macbook_pro = bool(re.search(r"macbook\s*pro", f"{title} {query}", re.I)) and \
            "air" not in _search_tokens(title) | _search_tokens(query)
        if plain_chip and screen == '16"':
            # 16" буває лише з M Pro / M Max — у назві, мабуть, пропущено; не змішуємо з дешевшим чипом
            chip += " Pro/Max ?"
        elif plain_chip and screen is None and macbook_pro:
            # MacBook Pro зі звичайним чипом: M1/M2 — лише 13", з M3 — лише 14"
            screen = '14"' if int(token[1:]) >= 3 else '13"'
        # SSD у класі не враховуємо: ціну Mac визначають чип, екран і RAM (SSD — дрібніша різниця,
        # а групи без нього більші й надійніші)
        parts = [chip, screen, _size_label(ram) if ram and ram <= 128 else None]
        return SEP.join(p for p in parts if p)

    # Windows-ноутбук: відеокарта · процесор · RAM
    gpu = (detect_gpu(title) or detect_gpu(_aspect(aspects, GPU_ASPECTS))
           or detect_gpu(_aspect(aspects, {DESC_GPU_ASPECT})))
    if gpu is None:
        lines = _search_tokens(query) | _search_tokens(title)
        gpu = "iGPU" if lines & IGPU_LINE_TERMS else UNKNOWN_GPU
    cpu = _cpu_class(title, cpu_text)
    if gpu == UNKNOWN_GPU and not cpu and not ram:
        return "unspecified"
    parts = [gpu, cpu, _ram_bucket(ram) if ram else None]
    return SEP.join(p for p in parts if p)


def _first(*pairs):
    for value, source in pairs:
        if value:
            return value, source
    return None, None


def class_sources(title, aspects=None, query=""):
    """Як бот визначив частини класу ноутбука: [(частина, значення або None, звідки)].
    Для «🛠 Нерозпізнані»: видно, чого бракує і де бот шукав."""
    aspects = aspects or {}
    t = _plain(title)
    spec = laptop_spec(title, aspects, query)
    parts = spec.split(SEP) if spec != "unspecified" else []
    ram = _first((_sizes(t)[0], "назва"), (_sizes(_aspect(aspects, RAM_ASPECTS) + " RAM")[0], "характеристики"),
                 (_sizes(_aspect(aspects, {DESC_RAM_ASPECT}) + " RAM")[0], "опис"))
    ram = (_size_label(ram[0]) if ram[0] else None, ram[1])
    if parts and _MAC_CHIP.fullmatch(parts[0]):
        _, chip_src = _first((extract_cpu_token(t), "назва"),
                             (extract_cpu_token(_aspect(aspects, CPU_ASPECTS)), "характеристики"),
                             (extract_cpu_token(_aspect(aspects, {DESC_CPU_ASPECT})), "опис"))
        screen = _first((_screen(t, {}), "назва"),
                        (_screen("", {k: v for k, v in aspects.items() if k in SCREEN_ASPECTS}), "характеристики"),
                        (aspects.get(DESC_SCREEN_ASPECT), "опис"))
        if screen[0] is None and len(parts) > 1 and parts[1].endswith('"'):
            screen = (parts[1], "типово для моделі")
        return [("чип", parts[0], chip_src), ("екран", *screen), ("RAM", *ram)]
    gpu = _first((detect_gpu(t), "назва"), (detect_gpu(_aspect(aspects, GPU_ASPECTS)), "характеристики"),
                 (detect_gpu(_aspect(aspects, {DESC_GPU_ASPECT})), "опис"))
    cpu = _first((_cpu_class(t), "назва"), (_cpu_class("", _aspect(aspects, CPU_ASPECTS)), "характеристики"),
                 (_cpu_class("", _aspect(aspects, {DESC_CPU_ASPECT})), "опис"))
    return [("відеокарта", *gpu), ("процесор", *cpu), ("RAM", *ram)]


def sources_line(sources):
    """«відеокарта: RTX 4060 (характеристики) · процесор: ❓ · RAM: 16GB (назва)»."""
    return " · ".join(f"{name}: {value} ({src})" if value else f"{name}: ❓" for name, value, src in sources)


def needs_aspects(spec):
    """Ноутбук, про відеокарту якого з назви нічого не відомо, — варто глянути характеристики."""
    return spec == "unspecified" or spec.startswith(UNKNOWN_GPU)


_FULL_CPU = re.compile(r"\bgen\b|Ryzen (?:AI )?\d \d000?\b|Core (?:Ultra )?\d \(S")
_RAM_PART = re.compile(r"\d+GB\+?")


def wants_aspects(spec):
    """Чи варто прочитати характеристики: відеокарта невідома (обов'язково) або в класі бракує
    покоління процесора чи пам'яті (для точнішої ціни). MacBook — якщо бракує екрана, RAM чи SSD."""
    if needs_aspects(spec):
        return True
    parts = spec.split(SEP)
    if re.fullmatch(r"M\d+( Pro| Max| Ultra| Pro/Max \?)?", parts[0]):
        # MacBook: чип · екран · RAM — бракує частини або чип під питанням
        return len(parts) < 3 or "?" in parts[0]
    has_cpu = any(_FULL_CPU.search(p) for p in parts[1:])
    has_ram = any(_RAM_PART.fullmatch(p) for p in parts[1:])
    return not (has_cpu and has_ram)


_MAC_CHIP = re.compile(r"M\d+( Pro| Max| Ultra| Pro/Max \?)?")
_WIN_GPU = re.compile(r"(RTX|GTX|RX|MX|Arc) .+|iGPU")
_RAM_LABEL = re.compile(r"\d+GB\+?")


def spec_kind(spec):
    """Тип класу: 'mac' (чип · …), 'win' (відеокарта · …) або None — не ноутбук."""
    first = (spec or "").split(SEP)[0]
    if _MAC_CHIP.fullmatch(first):
        return "mac"
    if _WIN_GPU.fullmatch(first) or first == UNKNOWN_GPU:
        return "win"
    return None


def group_scope(spec):
    """Для ширшої групи ноутбуків — чого вона не уточнює: «RTX 5050» → «усі процесори»,
    «RTX 3050 · i5 14 gen» → «уся RAM», «M4 Pro · 14"» → «уся RAM». Для повного класу чи
    не ноутбука — порожньо."""
    if not spec or spec in ("*", "unspecified"):
        return ""
    parts = spec.split(SEP)
    if _MAC_CHIP.fullmatch(parts[0]):
        if len(parts) >= 2 and not parts[1].endswith('"'):
            return ""          # екрана немає, а далі RAM/SSD — це точний клас, а не ширша група
        if len(parts) >= 3 and not _RAM_LABEL.fullmatch(parts[2]):
            return ""
        return ("усі екрани", "уся RAM", "")[min(len(parts), 3) - 1]
    if _WIN_GPU.fullmatch(parts[0]):
        if len(parts) >= 2 and _RAM_LABEL.fullmatch(parts[1]):
            return ""          # процесор невідомий, RAM відома — точний клас
        return ("усі процесори", "уся RAM", "")[min(len(parts), 3) - 1]
    return ""


def scoped_label(spec):
    """Назва групи з поясненням для ширших груп: «RTX 5050 · усі процесори»."""
    scope = group_scope(spec)
    return f"{spec}{SEP}{scope}" if scope else spec


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
