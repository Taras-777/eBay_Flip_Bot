"""
«⚙️ Налаштування» → «💰 Мін. прибуток»: з якого прибутку оголошення вважається вигідним.

Порядок: свій поріг товару (картка товару) → поріг категорії («🎮 Консолі», власні папки) → загальний.
Поріг категорії — у € або у % від ціни продажу.

mprof:show            — екран: загальний поріг і пороги категорій
mprof:<сума>          — зберегти загальний
cprof:<категорія>     — вибір порогу категорії
cprof:<категорія>:<v> — зберегти: «60» (€), «12p» (%), «def» (як загальний)
"""

import asyncio
import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from core.db import (
    get_category_profit,
    get_category_profits,
    get_min_profit,
    profit_rule_label,
    set_category_profit,
    set_min_profit,
)
from core.settings import MIN_PROFIT_CHOICES
from ui.access import require_access
from ui.panel import _ack_callback, show_panel
from ui.screen_folders import folder_names

CATEGORY_EUR_CHOICES = (15, 20, 30, 40, 50, 60, 80, 100, 150, 200)
CATEGORY_PCT_CHOICES = (5, 8, 10, 12, 15, 20, 25, 30)

HELP_TEXT = (
    "<b>Як це працює</b>\n"
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
        note = f"✅ Загальний мін. прибуток: <b>{float(choice):.0f}€</b>\n\n"
    await show_min_profit(update, context, note)


async def show_min_profit(update, context, note=""):
    chat_id = update.effective_chat.id
    current, rules, names = await asyncio.to_thread(
        lambda: (get_min_profit(chat_id), get_category_profits(chat_id), folder_names(chat_id)))
    buttons = [InlineKeyboardButton(("✅ " if v == current else "") + f"{v}€", callback_data=f"mprof:{v}")
               for v in MIN_PROFIT_CHOICES]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton("— Пороги категорій (натисни, щоб змінити) —", callback_data="mprof:show")])
    cat_buttons = [InlineKeyboardButton(f"{name}: " + (profit_rule_label(rules[key]) if key in rules
                                                        else "як загальний"),
                                        callback_data=f"cprof:{key}") for key, name in names.items()]
    rows += [cat_buttons[i:i + 2] for i in range(0, len(cat_buttons), 2)]
    rows.append([InlineKeyboardButton("◀️ До налаштувань", callback_data="menu:ebay_account")])
    await show_panel(
        update, context,
        f"{note}💰 <b>Мінімальний прибуток</b>\n"
        f"Загальний: <b>{current:.0f}€</b> — кнопки з сумами вгорі.\n\n{HELP_TEXT}",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
    )


@require_access
async def category_profit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cprof:<категорія> — вибір; cprof:<категорія>:<значення> — зберегти."""
    await _ack_callback(update)
    chat_id = update.effective_chat.id
    parts = update.callback_query.data.split(":")
    key = parts[1]
    names = await asyncio.to_thread(folder_names, chat_id)
    if key not in names:
        return await show_min_profit(update, context, "⚠️ Такої категорії вже немає.\n\n")
    name = html.escape(names[key])
    if len(parts) > 2:
        value = _parse_choice(parts[2])
        await asyncio.to_thread(set_category_profit, chat_id, key, value)
        saved = "як загальний" if value is None else profit_rule_label(get_category_profit(chat_id, key))
        return await show_min_profit(update, context, f"✅ {name}: <b>{saved}</b>\n\n")

    rule = await asyncio.to_thread(get_category_profit, chat_id, key)
    general = get_min_profit(chat_id)
    eur = [InlineKeyboardButton(("✅ " if rule == ("eur", float(v)) else "") + f"{v}€", callback_data=f"cprof:{key}:{v}")
           for v in CATEGORY_EUR_CHOICES]
    pct = [InlineKeyboardButton(("✅ " if rule == ("pct", float(v)) else "") + f"{v}%", callback_data=f"cprof:{key}:{v}p")
           for v in CATEGORY_PCT_CHOICES]
    rows = [[InlineKeyboardButton("— У євро —", callback_data=f"cprof:{key}")]]
    rows += [eur[i:i + 5] for i in range(0, len(eur), 5)]
    rows.append([InlineKeyboardButton("— У відсотках від ціни продажу —", callback_data=f"cprof:{key}")])
    rows += [pct[i:i + 4] for i in range(0, len(pct), 4)]
    rows.append([InlineKeyboardButton(("✅ " if rule is None else "") + f"↩️ Як загальний ({general:.0f}€)",
                                      callback_data=f"cprof:{key}:def")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="mprof:show")])
    current = "як загальний" if rule is None else profit_rule_label(rule)
    await show_panel(
        update, context,
        f"💰 <b>Мін. прибуток — {name}</b>: зараз <b>{current}</b>\n\n"
        "Діє на всі товари цієї категорії, крім тих, де в картці товару задано свій поріг.\n\n"
        "• <b>€</b> — однаковий прибуток для всіх товарів категорії.\n"
        "• <b>%</b> — частка від ціни продажу: 10% від 600€ = 60€, від 1500€ = 150€.\n"
        f"• <b>↩️ Як загальний</b> — без свого порогу ({general:.0f}€).",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
    )
