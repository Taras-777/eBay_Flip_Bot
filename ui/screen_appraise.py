"""
«💶 Оцінити товар»: за скільки можна продати товар — за посиланням на оголошення або за назвою.

menu:appraise   — попросити посилання / назву (діалог appraise_conv; можна надсилати кілька поспіль)
appr:track      — ➕ почати відстежувати оцінений товар
appr:observe    — 👀 спостерігати 7 днів (лише збирає продажі; потім звіт і товар — в архів)
"""

import asyncio
import html
import json
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from analysis.appraise import AppraiseError, appraise, group_label
from analysis.sales import speed_text
from core.settings import DEFAULT_CONDITION_IDS, LOCAL_TZ, MIN_SOLD_SAMPLE, SOLD_LOOKBACK_DAYS, log
from core.db import add_watch, find_duplicate_watch, get_watch, set_meta, set_observe
from ui.access import require_access
from ui.panel import _ack_callback, run_with_progress, show_panel
from ui.style import hint, menu_row, title

APPRAISE_INPUT = 0
OBSERVE_DAYS = 7

PROMPT = (
    title("💶", "Оцінити товар", "за скільки його можна продати на eBay.de") + "\n\n"
    "Надішли <b>посилання на оголошення</b> (можна коротке ebay.io/m/…) або <b>назву товару</b>, "
    "напр. <code>MacBook Pro 14 M3 Pro 18GB</code>.\n\n"
    + hint("Бот визначає клас товару (для MacBook — чип · екран · RAM, для консолей — модель і пам'ять) і бере:\n"
           f"1️⃣ <b>реальні продажі</b> 👤 приватних за {SOLD_LOOKBACK_DAYS} днів — з усіх товарів бота з таким самим "
           "товаром (твоїх, інших користувачів, з архіву), з «💡 Що перепродавати» і з раніше оцінених;\n"
           f"2️⃣ якщо їх менше {MIN_SOLD_SAMPLE} — додає <b>старіші продажі</b> (2–6 місяців, з «🗃 архіву продажів»): "
           "свіжі важать більше, але ціни могли змінитися;\n"
           "3️⃣ якщо продажів замало й так — <b>поточні оголошення</b> приватних того самого класу (нижня "
           "чверть цін; разовий пошук на eBay — 1 запит). Це оцінка: реальна ціна продажу зазвичай трохи нижча.\n"
           "Проданих товарів eBay звичайним програмам не показує (лише партнерам). Тому товар, якого бот ще не "
           "відстежує, він запам'ятовує й раз на добу збирає по ньому продажі — наступна оцінка буде точнішою. "
           "Або «👀 Спостерігати 7 днів» — бот тихо збере продажі й надішле звіт.")
)


def estimate_text(res):
    """Текст оцінки (і для екрана, і для звіту «👀 Спостерігати»)."""
    est = res["estimate"]
    lines = [title("💶", "Оцінка")]
    lines.append(f"<i>{html.escape(res['title'][:120])}</i>")
    spec = res.get("spec")
    if spec and spec != "unspecified":
        lines.append(f"🧩 Клас: {html.escape(spec)}")
    lines.append("")
    if not est.get("sale"):
        lines.append("❓ <b>Оцінити не вдалося</b> — бот не бачив ні продажів, ні достатньо оголошень цього класу.")
        if est.get("few_sold"):
            n, med, _ = est["few_sold"]
            lines.append(f"🛒 Помічено лише {n} продаж(і), медіана ~{med:.0f}€ — цього замало для оцінки.")
        lines.append("Спробуй назву точніше або «👀 Спостерігати 7 днів».")
        return "\n".join(lines)
    scope = html.escape(group_label(est["group"]))
    if est["source"] == "sold":
        lines.append(f"💶 Продати <b>~{est['sale']:.0f}€</b>")
        lines.append(f"📊 за {est['sold_count']} продажами за {SOLD_LOOKBACK_DAYS} днів · 👤 приватні, {scope}")
        extra = []
        if est.get("sold_range"):
            lo, hi = est["sold_range"]
            extra.append(f"зазвичай {lo:.0f}–{hi:.0f}€")
        if est.get("sold_days") is not None:
            extra.append(f"⏱ продається {speed_text(est['sold_days'])}")
        if extra:
            lines.append("↔️ " + " · ".join(extra))
    elif est["source"] == "sold_old":
        fresh = est["sold_count"] - est["old_count"]
        lines.append(f"💶 Продати <b>~{est['sale']:.0f}€</b> <i>(за старішими продажами)</i>")
        lines.append(f"📊 за {est['sold_count']} продажами: {fresh} за {SOLD_LOOKBACK_DAYS} днів + {est['old_count']} "
                     f"за продажами 2–6 місяців тому · 👤 приватні, {scope}")
        if est.get("sold_range"):
            lo, hi = est["sold_range"]
            lines.append(f"↔️ зазвичай {lo:.0f}–{hi:.0f}€")
        lines.append("⚠️ Частина продажів давня — ціни могли змінитися (електроніка з часом дешевшає). "
                     "Свіжі продажі важать більше.")
    else:
        lines.append(f"💶 Продати <b>~{est['sale']:.0f}€</b> <i>(оцінка за оголошеннями)</i>")
        lines.append(f"📊 за {est['listings_count']} поточними оголошеннями · 👤 приватні, {scope}")
        lines.append("⚠️ Продажів цього класу бот ще не бачив — реальна ціна продажу зазвичай трохи нижча.")
        if est.get("few_sold"):
            n, med, _ = est["few_sold"]
            lines.append(f"🛒 Уже помічено продажів: {n}, медіана ~{med:.0f}€ (для оцінки потрібно {MIN_SOLD_SAMPLE}).")
    if res.get("total") is not None and res.get("buy_limit") is not None:
        lines.append("")
        verdict = (f"✅ вигідно: прибуток <b>+{res['profit']:.0f}€</b>" if res["total"] <= res["buy_limit"]
                   else f"❌ невигідно: прибуток лише {res['profit']:+.0f}€")
        lines.append(f"🛒 Це оголошення: <b>{res['total']:.0f}€</b> з доставкою — {verdict}")
        lines.append(f"   купувати до {res['buy_limit']:.0f}€ (мін. прибуток {res['min_profit']:.0f}€, 7€ доставка покупцю)")
    if res.get("collecting"):
        lines.append("")
        lines.append("📡 Бот запам'ятав цей товар і раз на добу збиратиме по ньому продажі — "
                     "наступна оцінка буде точнішою.")
    return "\n".join(lines)


def _keyboard(res, chat_id):
    rows = []
    if res.get("url"):
        rows.append([InlineKeyboardButton("🔗 Відкрити оголошення", url=res["url"])])
    existing = find_duplicate_watch(chat_id, res["query"])
    if existing:
        rows.append([InlineKeyboardButton("📌 Мій товар", callback_data=f"watch_details:{existing['id']}")])
    else:
        rows.append([InlineKeyboardButton("➕ Почати відстежувати", callback_data="appr:track")])
        if res["estimate"].get("source") != "sold":
            rows.append([InlineKeyboardButton(f"👀 Спостерігати {OBSERVE_DAYS} днів", callback_data="appr:observe")])
    rows.append([InlineKeyboardButton("💶 Оцінити інший", callback_data="menu:appraise"), *menu_row()])
    return InlineKeyboardMarkup(rows)


@require_access
async def appraise_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """menu:appraise — попросити посилання або назву."""
    await _ack_callback(update)
    await show_panel(update, context, PROMPT, reply_markup=InlineKeyboardMarkup([menu_row()]),
                     parse_mode=ParseMode.HTML)
    return APPRAISE_INPUT


async def appraise_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Посилання / назва → оцінка. Діалог лишається відкритим: можна одразу надіслати наступне."""
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    context.user_data["panel_force_new"] = True   # відповідь — під повідомленням користувача
    try:
        res = await run_with_progress(update, context, "💶 ⏳ Оцінюю — читаю оголошення й ціни…", appraise, chat_id, text)
    except AppraiseError as e:
        await show_panel(update, context, f"⚠️ {html.escape(str(e))}\n\nНадішли інше посилання або назву.",
                         reply_markup=InlineKeyboardMarkup([menu_row()]), parse_mode=ParseMode.HTML)
        return APPRAISE_INPUT
    except Exception as e:
        log.exception("Не вдалося оцінити товар: %s", e)
        await show_panel(update, context, "⚠️ Не вдалося оцінити — eBay не відповів. Спробуй ще раз.",
                         reply_markup=InlineKeyboardMarkup([menu_row()]))
        return APPRAISE_INPUT
    context.user_data["appraisal"] = {k: res[k] for k in ("title", "query", "spec", "laptop")}
    text = estimate_text(res) + "\n\n<i>Можна одразу надіслати наступне посилання чи назву.</i>"
    await show_panel(update, context, text, reply_markup=await asyncio.to_thread(_keyboard, res, chat_id),
                     parse_mode=ParseMode.HTML)
    return APPRAISE_INPUT


async def appraise_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка замість посилання — вийти з діалогу й виконати її."""
    data = update.callback_query.data
    if data.startswith("appr:"):
        await appraise_action_callback(update, context)
    elif data == "menu:appraise":
        return await appraise_start(update, context)
    elif data.startswith("watch_details:"):
        from ui.screen_watch import watch_details_callback   # тут, щоб не було циклу імпортів
        await watch_details_callback(update, context)
    else:
        from ui.panel import show_main_menu
        await _ack_callback(update)
        await show_main_menu(update, context)
    return ConversationHandler.END


def _add_from_appraisal(chat_id, info, observe):
    """Новий товар з оціненого (як із «💡 Що перепродавати»). observe — «👀 Спостерігати»."""
    from analysis.shared_market import attach_shared_history   # тут: тягне мережеві модулі
    wid = add_watch(chat_id=chat_id, label=info["query"][:60], query=info["query"],
                    exclude="broken defekt teile parts kaputt", condition_ids=DEFAULT_CONDITION_IDS, categories=[])
    if observe:
        set_observe(wid, int(time.time()) + OBSERVE_DAYS * 86400)
        set_meta(f"observe:{wid}", json.dumps(info, ensure_ascii=False))
    try:
        attach_shared_history(get_watch(wid, chat_id))
    except Exception as e:
        log.warning("Не вдалося підключити спільну історію для «%s»: %s", info["query"], e)
    return wid


@require_access
async def observe_off_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """obsoff:<товар> — «✅ Відстежувати повністю»: зі спостереження — у звичайний товар."""
    await _ack_callback(update)
    from ui.screen_watch import _show_watch_details   # тут, щоб не було циклу імпортів
    wid = int(update.callback_query.data.split(":")[1])
    watch = get_watch(wid, update.effective_chat.id)
    if watch is None:
        return
    await asyncio.to_thread(set_observe, wid, None)
    await _show_watch_details(update, context, get_watch(wid, update.effective_chat.id),
                              note="✅ Тепер це звичайний товар: бот шукає знахідки й сповіщає про вигідні.")


def finish_observation(watch):
    """Строк «👀 Спостерігати» минув: (текст оцінки, id товару). Товар — в архів. Викликати з потоку."""
    from analysis.appraise import classify, estimate, matching_sources
    from core.db import archive_watch, get_meta
    try:
        info = json.loads(get_meta(f"observe:{watch['id']}") or "{}")
    except ValueError:
        info = {}
    title_text = info.get("title") or watch["label"]
    spec, laptop = (info["spec"], info["laptop"]) if info.get("spec") else classify(title_text, query=watch["query"])
    est = estimate(title_text, spec, laptop, query=watch["query"], live=False, sources=matching_sources(title_text))
    res = {"title": title_text, "spec": spec, "estimate": est, "total": None, "buy_limit": None}
    set_observe(watch["id"], None)
    archive_watch(watch["id"], watch["chat_id"])
    text = (f"👀 <b>Спостереження за «{html.escape(watch['label'])}» завершено</b>\n\n" + estimate_text(res)
            + "\n\n🗄 Товар перенесено в архів — історія збережена. Відстежувати далі — кнопка нижче.")
    return text, watch["id"]


@require_access
async def appraise_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """appr:track — почати відстежувати; appr:observe — спостерігати OBSERVE_DAYS днів."""
    await _ack_callback(update)
    info = context.user_data.get("appraisal")
    chat_id = update.effective_chat.id
    if not info:
        return await appraise_start(update, context)
    from ui.screen_watch import _show_watch_details   # тут, щоб не було циклу імпортів
    existing = await asyncio.to_thread(find_duplicate_watch, chat_id, info["query"])
    if existing:
        return await _show_watch_details(update, context, existing, note="📌 Цей товар уже відстежується.")
    observe = update.callback_query.data == "appr:observe"
    wid = await asyncio.to_thread(_add_from_appraisal, chat_id, info, observe)
    if observe:
        until = datetime.fromtimestamp(time.time() + OBSERVE_DAYS * 86400, LOCAL_TZ).strftime("%d.%m")
        note = (f"👀 <b>Спостерігаю до {until}</b>: бот лише збирає продажі (без знахідок і сповіщень), а потім "
                "надішле оцінку за продажами й перемістить товар в «🗄 Архів». Відстежувати повністю — кнопка нижче.")
    else:
        note = "✅ Додано. Ціни з'являться після найближчого ринкового сканування (до години)."
    await _show_watch_details(update, context, get_watch(wid, chat_id), note=note)
