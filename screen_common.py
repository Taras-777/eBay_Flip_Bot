"""
Спільні дрібниці екранів: клавіатури категорій і характеристик, формат часу.
"""

import config
import time
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from settings import LOCAL_TZ
from textparse import aspect_label, category_label, is_accessory_category, plural


def _ebay_configured():
    return bool(config.EBAY_CLIENT_ID) and "PUT_YOUR" not in config.EBAY_CLIENT_ID


def _category_keyboard(options, prefix, selected=(), extra_rows=None):
    """Перемикачі категорій: f'{prefix}{index}' — позначити/зняти,
    f'{prefix}done' — зберегти вибір, f'{prefix}all' — без обмеження категорією.
    Категорії аксесуарів/запчастин позначені ⚠️ і показані останніми."""
    rows = []
    order = sorted(range(len(options)), key=lambda i: is_accessory_category(options[i]["name"]))
    for idx in order:
        opt = options[idx]
        shown = category_label(opt["name"])
        label = shown if len(shown) <= 34 else shown[:31] + "…"
        mark = "☑️" if opt["id"] in selected else "⬜"
        warn = " ⚠️" if is_accessory_category(opt["name"]) else ""
        rows.append([InlineKeyboardButton(f"{mark} {label} ({opt['count']}){warn}", callback_data=f"{prefix}{idx}")])
    rows.append([InlineKeyboardButton(f"✅ Готово ({len(selected)} обрано)", callback_data=f"{prefix}done")])
    rows.append([InlineKeyboardButton("🌐 Усі категорії (без обмеження)", callback_data=f"{prefix}all")])
    for row in extra_rows or []:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _aspect_keyboard(watch_id, options, selected, extra_rows=None):
    """Перемикачі характеристик ☑️/⬜ + збереження, авто, без вимоги."""
    rows = []
    for idx, opt in enumerate(options):
        mark = "☑️" if opt["name"] in selected else "⬜"
        badge = " ❗" if opt.get("required") else ""
        shown = aspect_label(opt["name"])
        shown = shown if len(shown) <= 36 else shown[:35] + "…"
        rows.append([InlineKeyboardButton(f"{mark} {shown}{badge}", callback_data=f"tglasp:{watch_id}:{idx}")])
    rows.append([InlineKeyboardButton(f"💾 Зберегти вибір ({len(selected)})", callback_data=f"saveasp:{watch_id}")])
    rows.append([
        InlineKeyboardButton("🤖 Авто", callback_data=f"setasp:{watch_id}:auto"),
        InlineKeyboardButton("🚫 Без вимоги", callback_data=f"setasp:{watch_id}:none"),
    ])
    for row in extra_rows or []:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


ASPECT_CHOICE_TEXT = (
    "Познач одну або кілька характеристик і натисни «💾 Зберегти». Оголошення враховуватиметься, "
    "лише якщо в ньому заповнені ВСІ позначені характеристики — інакше вважається аксесуаром.\n"
    "❗ — обов'язкова в цій категорії (продавець не може її пропустити).\n"
    "🤖 Авто — для телефонів, ноутбуків і консолей бот сам вимагає обсяг пам'яті; 🚫 — без вимоги.\n\n"
    "⚠️ Якщо характеристик зазвичай немає в назві, бот перевірятиме характеристики кожного "
    "оголошення — це додаткові запити до eBay (з кешем і лімітом). "
    "Оголошення з «Kompatible Marke/Modell» відкидаються завжди."
)


def _ago(ts):
    days = int((time.time() - ts) // 86400)
    if days <= 0:
        return "сьогодні"
    if days == 1:
        return "вчора"
    return f"{plural(days, 'день', 'дні', 'днів')} тому"


def _listed(ts):
    """Коли оголошення виставили: «сьогодні о 14:02», «вчора о 09:15», «27.09.2026 (3 дні тому)»."""
    if not ts:
        return ""
    moment = datetime.fromtimestamp(ts, LOCAL_TZ)
    days = (datetime.now(LOCAL_TZ).date() - moment.date()).days
    if days <= 0:
        return f"сьогодні о {moment:%H:%M}"
    if days == 1:
        return f"вчора о {moment:%H:%M}"
    return f"{moment:%d.%m.%Y} ({plural(days, 'день', 'дні', 'днів')} тому)"


def _when(ts):
    moment = datetime.fromtimestamp(ts, LOCAL_TZ)
    days = (datetime.now(LOCAL_TZ).date() - moment.date()).days
    if days == 0:
        return f"сьогодні о {moment:%H:%M}"
    if days == 1:
        return f"вчора о {moment:%H:%M}"
    return f"{moment:%d.%m} о {moment:%H:%M}"
