"""
«⚙️ Налаштування» → «💰 Мін. прибуток»: з якого прибутку оголошення вважається вигідним.

Порядок: свій поріг товару (картка товару) → поріг категорії («🎮 Консолі», власні папки) → загальний.
Поріг категорії — у € або у % від ціни продажу.

mprof:show            — екран: загальний поріг і пороги категорій
mprof:<сума>          — зберегти загальний
cprof:<категорія>     — вибір порогу категорії
cprof:<категорія>:<v> — зберегти: «60» (€), «12p» (%), «def» (як загальний)
cprofin:<категорія>:eur|pct — ввести своє значення повідомленням (діалог profit_conv)
"""

import asyncio
import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from analysis.market import RESALE_GROUP, max_buy_price, watch_group
from core.db import (
    get_category_profit,
    get_category_profits,
    get_market_stats,
    get_min_profit,
    list_folders,
    list_watches,
    min_profit_rule,
    profit_needed,
    profit_rule_label,
    set_category_profit,
    set_min_profit,
)
from core.settings import MIN_PROFIT_CHOICES
from ui.access import require_access
from ui.panel import _ack_callback, show_panel
from ui.screen_folders import folder_names
from ui.style import grid, hint, note_line, title

CATEGORY_EUR_CHOICES = (15, 20, 30, 40, 50, 60, 80, 100, 150, 200)
CATEGORY_PCT_CHOICES = (5, 8, 10, 12, 15, 20, 25, 30)
CATEGORY_LIST_MAX = 15           # скільки товарів категорії показати в екрані порогу
PROFIT_INPUT = 0                 # діалог: чекаємо число
CUSTOM_EUR_RANGE = (1, 5000)     # своє значення в €
CUSTOM_PCT_RANGE = (1, 90)       # своє значення у %

HELP_TEXT = (
    "Бот пропонує оголошення, лише якщо після перепродажу лишається щонайменше стільки прибутку:\n"
    "<code>купувати до = ціна продажу − 7€ доставки − мін. прибуток</code>\n\n"
    "<b>Звідки береться поріг</b> (бот перевіряє по черзі):\n"
    "1️⃣ свій поріг товару — у картці товару «💰 Мін. прибуток»;\n"
    "2️⃣ поріг категорії — кнопки нижче (🎮 Консолі, 💻 Ноутбуки, твої папки…);\n"
    "3️⃣ загальний — для всього іншого.\n\n"
    "<b>€ чи %</b> (лише для категорій):\n"
    "• <b>€</b> — однаково для всіх товарів категорії. Консолі 60€: PS5 за 560€ → купувати до 493€.\n"
    "• <b>%</b> — від ціни продажу: дорожчий товар вимагає більшого прибутку. Ноутбуки 12%: "
    "MacBook за 868€ → прибуток від ~104€ (купувати до ~757€), ноутбук за 1500€ → від 180€.\n"
    "% зручний, коли в категорії і дешеві, і дорогі речі. Для дешевих товарів % дає малий поріг "
    "(10% від 80€ = 8€) — таким краще поставити € або свій поріг у картці товару.\n\n"
    "Пороги діють і на «🔥 Вигідні пропозиції», аукціони, знижки та «💡 Що варто перепродавати». "
    "Прибуток рахується за ціною оголошення — торг (🎯) лише бонус. Після зміни бот одразу прибирає "
    "зі «🔥 Вигідні пропозиції» все, що нижче нового порогу, а раніше відкинуті оголошення оцінює заново."
)


def _parse_choice(raw):
    """«60» → «60», «12p» → «12%», «def» → None."""
    if raw == "def":
        return None
    return f"{float(raw[:-1]):g}%" if raw.endswith("p") else f"{float(raw):g}"


@require_access
async def min_profit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mprof:show — екран; mprof:<сума> — зберегти загальний поріг."""
    await _ack_callback(update)
    chat_id = update.effective_chat.id
    choice = update.callback_query.data.split(":")[1]
    note = ""
    if choice != "show":
        await asyncio.to_thread(set_min_profit, chat_id, float(choice))
        note = f"✅ Загальний мін. прибуток: <b>{float(choice):.0f}€</b>"
    await show_min_profit(update, context, note)


def _rule_short(rules, key):
    return profit_rule_label(rules[key]) if key in rules else "загальний"


async def show_min_profit(update, context, note=""):
    chat_id = update.effective_chat.id
    current, rules, names = await asyncio.to_thread(
        lambda: (get_min_profit(chat_id), get_category_profits(chat_id), folder_names(chat_id)))
    buttons = [InlineKeyboardButton(("✅ " if v == current else "") + f"{v}€", callback_data=f"mprof:{v}")
               for v in MIN_PROFIT_CHOICES]
    rows = grid(buttons, 4)
    rows += grid([InlineKeyboardButton(f"{name} · {_rule_short(rules, key)}", callback_data=f"cprof:{key}")
                  for key, name in names.items()])
    rows.append([InlineKeyboardButton("◀️ Налаштування", callback_data="menu:ebay_account")])
    categories = "\n".join(f"{name} — <b>{profit_rule_label(rules[key])}</b>" if key in rules
                           else f"{name} — як загальний" for key, name in names.items())
    text = (note_line(note)
            + title("💰", "Мінімальний прибуток", "скільки щонайменше заробити на одній речі") + "\n\n"
            + f"🌐 Загальний: <b>{current:.0f}€</b> <i>(кнопки з сумами нижче)</i>\n\n"
            + "🗂 <b>Пороги категорій</b> <i>(натисни категорію, щоб змінити)</i>\n" + categories + "\n\n"
            + hint(HELP_TEXT))
    await show_panel(update, context, text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)


_SOURCE_NOTE = {"own": "свій", "category": "категорії", "general": "загальний"}


def category_watch_lines(chat_id, key):
    """Товари категорії для орієнтиру: типова ціна продажу (👤 приватні, усі конфігурації), поріг,
    який зараз діє, і «купувати до». Від найдорожчого."""
    custom = {f["id"] for f in list_folders(chat_id)}
    rows = []
    for w in list_watches(chat_id=chat_id):
        if watch_group(w, custom) != key:
            continue
        stats = {(st["cond_group"], st["spec_group"]): st for st in get_market_stats(w["id"])}
        stat = stats.get((RESALE_GROUP, "*")) or stats.get(("new", "*"))
        sale = (stat["sale_price"] or stat["median_price"]) if stat else None
        rule, source = min_profit_rule(w)
        rows.append((sale or 0, w["label"], sale, rule, source))
    rows.sort(key=lambda row: -row[0])
    lines = []
    for _, label, sale, rule, source in rows[:CATEGORY_LIST_MAX]:
        threshold = profit_rule_label(rule)
        if sale:
            needed = profit_needed(rule, sale)
            if rule[0] == "pct":
                threshold += f" = {needed:.0f}€"
            lines.append(f"• <b>{html.escape(label)}</b> — продаж ~{sale:.0f}€ · поріг {threshold} "
                         f"({_SOURCE_NOTE[source]}) · купувати до ~{max_buy_price(sale, needed):.0f}€")
        else:
            lines.append(f"• <b>{html.escape(label)}</b> — ціни ще не пораховані · поріг {threshold} "
                         f"({_SOURCE_NOTE[source]})")
    if len(rows) > CATEGORY_LIST_MAX:
        lines.append(f"…і ще {len(rows) - CATEGORY_LIST_MAX}")
    return lines


@require_access
async def category_profit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cprof:<категорія> — вибір; cprof:<категорія>:<значення> — зберегти."""
    await _ack_callback(update)
    chat_id = update.effective_chat.id
    parts = update.callback_query.data.split(":")
    key = parts[1]
    names = await asyncio.to_thread(folder_names, chat_id)
    if key not in names:
        return await show_min_profit(update, context, "⚠️ Такої категорії вже немає.")
    name = html.escape(names[key])
    if len(parts) > 2:
        value = _parse_choice(parts[2])
        await asyncio.to_thread(set_category_profit, chat_id, key, value)
        saved = "як загальний" if value is None else profit_rule_label(get_category_profit(chat_id, key))
        return await show_min_profit(update, context, f"✅ {name}: <b>{saved}</b>")

    rule, watch_lines = await asyncio.to_thread(
        lambda: (get_category_profit(chat_id, key), category_watch_lines(chat_id, key)))
    general = get_min_profit(chat_id)
    eur = [InlineKeyboardButton(("✅ " if rule == ("eur", float(v)) else "") + f"{v}€", callback_data=f"cprof:{key}:{v}")
           for v in CATEGORY_EUR_CHOICES]
    pct = [InlineKeyboardButton(("✅ " if rule == ("pct", float(v)) else "") + f"{v}%", callback_data=f"cprof:{key}:{v}p")
           for v in CATEGORY_PCT_CHOICES]
    rows = [[InlineKeyboardButton("💶 У євро · ✏️ ввести свою суму", callback_data=f"cprofin:{key}:eur")]]
    rows += grid(eur, 5)
    rows.append([InlineKeyboardButton("📊 У % від ціни продажу · ✏️ ввести свій", callback_data=f"cprofin:{key}:pct")])
    rows += grid(pct, 4)
    rows.append([InlineKeyboardButton(("✅ " if rule is None else "") + f"↩️ Як загальний ({general:.0f}€)",
                                      callback_data=f"cprof:{key}:def")])
    rows.append([InlineKeyboardButton("◀️ Мін. прибуток", callback_data="mprof:show")])
    current = f"як загальний ({general:.0f}€)" if rule is None else profit_rule_label(rule)
    goods = (("📋 <b>Товари в категорії</b> <i>(типова ціна продажу, 👤 приватні)</i>\n" + "\n".join(watch_lines))
             if watch_lines else "📋 У цій категорії поки немає товарів.")
    help_text = ("• <b>€</b> — однаковий прибуток для всіх товарів категорії.\n"
                 "• <b>%</b> — частка від ціни продажу: 10% від 600€ = 60€, від 1500€ = 150€.\n"
                 f"• <b>↩️ Як загальний</b> — без свого порогу, слідує за загальним ({general:.0f}€).\n"
                 "• Потрібного значення немає серед кнопок — натисни «✏️» і напиши своє число.\n"
                 "• Поріг «свій» задано в картці товару — зміна порогу категорії його не чіпає.")
    await show_panel(
        update, context,
        title("💰", f"Мін. прибуток · {name}") + f"\nЗараз: <b>{current}</b>\n"
        "<i>Діє на всі товари категорії, крім тих, де в картці задано свій поріг.</i>\n\n"
        + goods + "\n\n" + hint(help_text, "ℹ️ € чи %"),
        reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
    )


@require_access
async def profit_input_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cprofin:<категорія>:eur|pct — попросити число."""
    await _ack_callback(update)
    _, key, kind = update.callback_query.data.split(":")
    names = await asyncio.to_thread(folder_names, update.effective_chat.id)
    if key not in names:
        await show_min_profit(update, context, "⚠️ Такої категорії вже немає.")
        return ConversationHandler.END
    context.user_data["profit_input"] = (key, kind)
    low, high = CUSTOM_EUR_RANGE if kind == "eur" else CUSTOM_PCT_RANGE
    what = (f"суму в <b>євро</b> (від {low} до {high}), напр. <code>75</code>" if kind == "eur" else
            f"<b>відсоток</b> від ціни продажу (від {low} до {high}), напр. <code>18</code> або <code>7.5</code>")
    rows = [[InlineKeyboardButton("❌ Скасувати", callback_data=f"cprof:{key}")]]
    await show_panel(update, context,
                     title("✏️", f"Свій поріг · {html.escape(names[key])}") + f"\n\nНапиши {what}.",
                     reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
    return PROFIT_INPUT


def _parse_number(text):
    try:
        return float((text or "").strip().replace(",", ".").rstrip("€%").strip())
    except ValueError:
        return None


async def profit_input_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Число від користувача — зберегти як поріг категорії."""
    key, kind = context.user_data.get("profit_input") or (None, None)
    if key is None:
        return ConversationHandler.END
    value = _parse_number(update.message.text)
    low, high = CUSTOM_EUR_RANGE if kind == "eur" else CUSTOM_PCT_RANGE
    context.user_data["panel_force_new"] = True   # відповідь — під повідомленням з числом
    if value is None or not low <= value <= high:
        unit = "€" if kind == "eur" else "%"
        rows = [[InlineKeyboardButton("◀️ Назад", callback_data=f"cprof:{key}")]]
        await show_panel(update, context, f"⚠️ Потрібне число від {low} до {high}{unit}. Напиши ще раз.",
                         reply_markup=InlineKeyboardMarkup(rows))
        return PROFIT_INPUT
    context.user_data.pop("profit_input", None)
    stored = f"{value:g}%" if kind == "pct" else f"{value:g}"
    await asyncio.to_thread(set_category_profit, update.effective_chat.id, key, stored)
    name = html.escape((await asyncio.to_thread(folder_names, update.effective_chat.id)).get(key, ""))
    await show_min_profit(update, context, f"✅ {name}: <b>{profit_rule_label(get_category_profit(update.effective_chat.id, key))}</b>")
    return ConversationHandler.END


async def profit_input_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Інша кнопка замість числа — вийти з діалогу й виконати її."""
    context.user_data.pop("profit_input", None)
    data = update.callback_query.data
    if data.startswith("cprof:"):
        await category_profit_callback(update, context)
    elif data.startswith("mprof:"):
        await min_profit_callback(update, context)
    else:
        from ui.panel import show_main_menu
        await show_main_menu(update, context)
    return ConversationHandler.END
