"""
Екран «🛠 Нерозпізнані» (кнопка на екрані товару, лише коли такі оголошення є).

unkall:0                        — з головного меню: товари з кількістю нерозпізнаних
unk:<товар>:<сторінка>[:m|:w]   — список (по 5, до 50 найцікавіших); m/w — відкрито з меню / з товару
unkm:<товар>:<№>                — «✏️ Вказати вручну»: варіанти класу
unks:<товар>:<№>:<варіант>      — зберегти обраний клас
unkk / unkr / unkf:<товар>:<№>  — «👌 Залишити як є» / «❌ Інший товар» / «🚩 Для розробника»
unkrep:<товар>, unkrepc:<товар> — звіт для розробника / очистити його
"""

import asyncio
import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from db import get_watch
from learning import learned_words_note, reject_and_learn
from panel import _ack_callback, run_with_progress, show_panel
from access import require_access
from textparse import _group_label, plural
from unrecognized import (
    MAX_UNRECOGNIZED,
    apply_manual_class,
    class_options,
    clear_report,
    developer_report,
    flag_for_developer,
    keep_as_is,
    drop_unavailable,
    unrecognized_by_watch,
    unrecognized_items,
)

PER_PAGE = 5


def _key(watch_id):
    return f"unk_{watch_id}"


def _card(n, it):
    copies = f" · ×{it['copies']} однакових" if it.get("copies", 1) > 1 else ""
    lines = [f"<b>{n}. {html.escape((it['title'] or 'без назви')[:140])}</b>",
             f"💶 {it['price']:.0f} €{copies}",
             f"🔍 {html.escape(it['info'])}"]
    if it.get("ref"):
        stat, sale = it["ref"]
        pct = (it["price"] - sale) / sale * 100
        lines.append(f"📊 {pct:+.0f}% від ціни групи «{html.escape(_group_label(stat['cond_group'], stat['spec_group']))}»"
                     f" (~{sale:.0f}€)")
    if it.get("url"):
        lines.append(f'<a href="{html.escape(it["url"])}">🔗 Відкрити на eBay</a>')
    return "\n".join(lines)


async def _render(update, context, watch, page=0, note=""):
    items = await asyncio.to_thread(unrecognized_items, watch)
    pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    page = min(max(page, 0), pages - 1)
    # Продані й завершені не показуємо: оголошення сторінки — перевірка на eBay (не частіше раз на 30 хв)
    shown = items[page * PER_PAGE:(page + 1) * PER_PAGE]
    removed = await run_with_progress(update, context, "🛠 ⏳ Перевіряю на eBay, чи оголошення ще продаються…",
                                      drop_unavailable, watch, shown) if shown else 0
    if removed:
        items = await asyncio.to_thread(unrecognized_items, watch)
        pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
        page = min(page, pages - 1)
        gone = f"🗑 Прибрано вже проданих чи завершених: {removed}"
        note = f"{note}\n{gone}" if note else gone
    _, flagged = await asyncio.to_thread(developer_report, watch)
    context.user_data[_key(watch["id"])] = {"items": items, "page": page}
    context.user_data[_key(watch["id"])]["page"] = page
    shown = items[page * PER_PAGE:(page + 1) * PER_PAGE]
    wid = watch["id"]

    head = f"🛠 <b>Нерозпізнані — {html.escape(watch['label'])}</b> ({len(items)}"
    head += f", показую до {MAX_UNRECOGNIZED} найцікавіших)" if len(items) >= MAX_UNRECOGNIZED else ")"
    if pages > 1:
        head += f" · сторінка {page + 1} з {pages}"
    lines = [head]
    if note:
        lines.append(note)
    lines.append("<i>Бот прочитав назву, характеристики й опис, але клас визначив не повністю — такі оголошення "
                 "він не пропонує як знахідки. Найдешевші відносно ціни групи — вгорі.\n"
                 "✏️ вказати клас · 👌 залишити як є · ❌ інший товар · 🚩 приклад для розробника</i>")
    rows = []
    for n, it in enumerate(shown, page * PER_PAGE + 1):
        lines.append(_card(n, it))
        i = n - 1
        rows.append([InlineKeyboardButton(f"✏️ {n}", callback_data=f"unkm:{wid}:{i}"),
                     InlineKeyboardButton(f"👌 {n}", callback_data=f"unkk:{wid}:{i}"),
                     InlineKeyboardButton(f"❌ {n}", callback_data=f"unkr:{wid}:{i}"),
                     InlineKeyboardButton(f"🚩 {n}", callback_data=f"unkf:{wid}:{i}")])
    if not items:
        lines.append("Усе розпізнано 👍")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Попередні", callback_data=f"unk:{wid}:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Наступні ▶️", callback_data=f"unk:{wid}:{page + 1}"))
    if nav:
        rows.append(nav)
    if flagged:
        rows.append([InlineKeyboardButton(f"📋 Для розробника ({flagged})", callback_data=f"unkrep:{wid}")])
    if context.user_data.get("unk_origin") == "menu":
        rows.append([InlineKeyboardButton("◀️ До всіх товарів", callback_data="unkall:0"),
                     InlineKeyboardButton("📌 До товару", callback_data=f"watch_details:{wid}")])
    else:
        rows.append([InlineKeyboardButton("◀️ До товару", callback_data=f"watch_details:{wid}")])
    await show_panel(update, context, "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
                     parse_mode=ParseMode.HTML)


async def _render_all(update, context):
    """З головного меню: товари, де є нерозпізнані оголошення, — від найбільшої кількості."""
    per_watch = await asyncio.to_thread(unrecognized_by_watch, update.effective_chat.id)
    total = sum(n for _, n in per_watch)
    lines = [f"🛠 <b>Нерозпізнані оголошення</b> ({total})",
             "<i>Оголошення, клас яких бот не визначив повністю навіть після назви, характеристик і опису. "
             "Обери товар — там можна вказати клас вручну, залишити як є, прибрати чи надіслати "
             "приклад розробнику.</i>"]
    if not per_watch:
        lines.append("Усе розпізнано 👍")
    rows = [[InlineKeyboardButton(f"📦 {w['label']} ({n})"[:60], callback_data=f"unk:{w['id']}:0:m")]
            for w, n in per_watch]
    rows.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:home")])
    await show_panel(update, context, "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
                     parse_mode=ParseMode.HTML)


def _item(context, watch_id, index):
    state = context.user_data.get(_key(watch_id)) or {}
    items = state.get("items") or []
    return (items[index] if 0 <= index < len(items) else None), state.get("page", 0)


@require_access
async def unknown_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ack_callback(update)
    parts = update.callback_query.data.split(":")
    if parts[0] == "unkall":
        context.user_data["unk_origin"] = "menu"
        return await _render_all(update, context)
    action, wid = parts[0], int(parts[1])
    watch = get_watch(wid, update.effective_chat.id)
    if watch is None:
        return

    if action == "unk":
        if len(parts) > 3:   # звідки відкрито — щоб «◀️» вела назад туди ж
            context.user_data["unk_origin"] = "menu" if parts[3] == "m" else "watch"
        return await _render(update, context, watch, int(parts[2]) if len(parts) > 2 else 0)
    if action == "unkrep":
        text, count = await asyncio.to_thread(developer_report, watch)
        rows = [[InlineKeyboardButton("🗑 Очистити (вже надіслав)", callback_data=f"unkrepc:{wid}")],
                [InlineKeyboardButton("◀️ Назад", callback_data=f"unk:{wid}:0")]]
        body = (f"📋 <b>Приклади для розробника ({count})</b>\nСкопіюй текст нижче і надішли розробнику.\n\n"
                f"<pre>{html.escape(text[:3500])}</pre>")
        return await show_panel(update, context, body, reply_markup=InlineKeyboardMarkup(rows),
                                parse_mode=ParseMode.HTML)
    if action == "unkrepc":
        await asyncio.to_thread(clear_report, watch)
        return await _render(update, context, watch, 0, note="🗑 Приклади для розробника очищено.")

    item, page = _item(context, wid, int(parts[2]))
    if item is None:
        return await _render(update, context, watch, 0, note="Список оновився — спробуй ще раз.")

    if action == "unkm":
        options = await asyncio.to_thread(class_options, watch, item)
        context.user_data[f"unkopt_{wid}"] = options
        rows = [[InlineKeyboardButton(opt[:60], callback_data=f"unks:{wid}:{parts[2]}:{k}")]
                for k, opt in enumerate(options)]
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"unk:{wid}:{page}")])
        text = (f"✏️ <b>Вказати клас вручну</b>\n\n{_card(int(parts[2]) + 1, item)}\n\n"
                + ("Обери, що це за конфігурація (варіанти — з оголошень цього товару, "
                   "що збігаються з уже відомим):" if options else
                   "Повних класів у цьому товарі ще немає — нема з чого обрати. Спробуй пізніше."))
        return await show_panel(update, context, text, reply_markup=InlineKeyboardMarkup(rows),
                                parse_mode=ParseMode.HTML)
    if action == "unks":
        options = context.user_data.get(f"unkopt_{wid}") or []
        k = int(parts[3])
        if not 0 <= k < len(options):
            return await _render(update, context, watch, page, note="Список оновився — спробуй ще раз.")
        await asyncio.to_thread(apply_manual_class, watch, item["item_id"], options[k])
        note = (f"✏️ Клас «{html.escape(options[k])}» збережено. Оголошення враховується в цінах "
                "і буде оцінене як можлива знахідка.")
    elif action == "unkk":
        await asyncio.to_thread(keep_as_is, watch, item)
        note = "👌 Залишено як є — більше не показуватиму."
    elif action == "unkr":
        words = await asyncio.to_thread(reject_and_learn, watch, item["item_id"], item["title"] or "")
        note = "❌ Прибрано як інший товар."
        if words:
            note += "\n" + html.escape(learned_words_note(words))
    elif action == "unkf":
        await asyncio.to_thread(flag_for_developer, watch, item)
        _, count = await asyncio.to_thread(developer_report, watch)
        note = (f"🚩 Додано до прикладів для розробника ({plural(count, 'приклад', 'приклади', 'прикладів')}). "
                "Надішли їх кнопкою «📋 Для розробника».")
    else:
        return
    await _render(update, context, watch, page, note=note)
