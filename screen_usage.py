"""
📡 Статистика запитів (у «⚙️ Налаштування»): скільки запитів до eBay бот зробив за кожен день.

Дані — власний лічильник бота (таблиця api_usage, зберігається API_USAGE_RETENTION_DAYS днів).
Доба лічильника — за UTC (о 02:00 влітку / 01:00 взимку за Берліном), тож вона трохи не
збігається з добою ліміту eBay (скидається о ~09:00). Без запитів до eBay — лише база.
"""

import asyncio
from datetime import date, datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from settings import API_USAGE_RETENTION_DAYS, DAILY_BROWSE_BUDGET, TRADING_DAILY_BUDGET
from db import api_usage_by_day
from panel import _ack_callback, show_panel

DAYS_PER_PAGE = 10
BAR_WIDTH = 10

# Чим були запити Trading API (решта — «інше»)
TRADING_PARTS = (("trading:watch", "мої товари"), ("trading:discovery", "💡"), ("trading:deals", "🔥"))


def _bar(used, budget):
    """▰▰▰▰▱▱▱▱▱▱ — частка добового бюджету."""
    filled = min(BAR_WIDTH, round(used / budget * BAR_WIDTH)) if budget else 0
    return "▰" * filled + "▱" * (BAR_WIDTH - filled)


def _day_label(day, today):
    d = date.fromisoformat(day)
    delta = (today - d).days
    name = "сьогодні" if delta == 0 else "вчора" if delta == 1 else None
    return f"{d:%d.%m}" + (f" ({name})" if name else "")


def _day_block(day, counts, today):
    browse = counts.get("browse", 0)
    trading = counts.get("trading", 0)
    lines = [f"<b>{_day_label(day, today)}</b>",
             f"  🔎 Browse: <b>{browse}</b> {_bar(browse, DAILY_BROWSE_BUDGET)} {browse / DAILY_BROWSE_BUDGET:.0%}"]
    details = []
    if counts.get("item"):
        details.append(f"оголошення (getItem): {counts['item']}")
    if counts.get("browse:deals"):
        details.append(f"перевірка 🔥: {counts['browse:deals']}")
    if details:
        lines.append("     з них " + " · ".join(details))
    if trading:
        parts = [f"{label} {counts[key]}" for key, label in TRADING_PARTS if counts.get(key)]
        lines.append(f"  🧾 Trading: <b>{trading}</b> {_bar(trading, TRADING_DAILY_BUDGET)} "
                     f"{trading / TRADING_DAILY_BUDGET:.0%}" + (f"\n     {' · '.join(parts)}" if parts else ""))
    if counts.get("taxonomy"):
        lines.append(f"  🗂 Категорії (Taxonomy): {counts['taxonomy']}")
    return "\n".join(lines)


def usage_text(page=0, today=None):
    """Текст екрана і кількість сторінок."""
    today = today or datetime.now(timezone.utc).date()
    data = api_usage_by_day(API_USAGE_RETENTION_DAYS)
    days = list(data)
    pages = max(1, (len(days) + DAYS_PER_PAGE - 1) // DAYS_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    lines = ["📡 <b>Статистика запитів до eBay</b>",
             f"<i>Ліміт бота на добу: Browse {DAILY_BROWSE_BUDGET}, Trading {TRADING_DAILY_BUDGET}. "
             "Підрахунок бота; доба — за UTC (з 02:00 за Берліном влітку, з 01:00 взимку).</i>"]
    if not days:
        lines.append("\nЗапитів ще не записано.")
        return "\n".join(lines), 1
    full = [data[d].get("browse", 0) for d in days if d != today.isoformat()]
    if full:
        lines.append(f"\nУ середньому за повну добу: Browse <b>{sum(full) / len(full):.0f}</b>, "
                     f"найбільше {max(full)} ({len(full)} дн.)")
    # Сторінка 0 — останні дні; усередині сторінки — від старішого до сьогоднішнього (сьогодні внизу)
    for day in reversed(days[page * DAYS_PER_PAGE:(page + 1) * DAYS_PER_PAGE]):
        lines.append("\n" + _day_block(day, data[day], today))
    if pages > 1:
        lines.append(f"\n<i>Сторінка {page + 1} з {pages}</i>")
    return "\n".join(lines), pages


def usage_keyboard(page, pages):
    rows = []
    nav = []
    if page < pages - 1:
        nav.append(InlineKeyboardButton("◀️ Старіші", callback_data=f"apistat:{page + 1}"))
    if page > 0:
        nav.append(InlineKeyboardButton("Новіші ▶️", callback_data=f"apistat:{page - 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔄 Оновити", callback_data=f"apistat:{page}")])
    rows.append([InlineKeyboardButton("◀️ До налаштувань", callback_data="menu:ebay_account")])
    return InlineKeyboardMarkup(rows)


async def usage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """apistat:<сторінка> — статистика запитів по днях (лише власник)."""
    from access import is_owner
    await _ack_callback(update)
    if not is_owner(update.effective_user.id):
        return
    try:
        page = int(update.callback_query.data.split(":")[1])
    except (IndexError, ValueError):
        page = 0
    text, pages = await asyncio.to_thread(usage_text, page)
    page = min(max(page, 0), pages - 1)
    await show_panel(update, context, text, reply_markup=usage_keyboard(page, pages), parse_mode=ParseMode.HTML)
