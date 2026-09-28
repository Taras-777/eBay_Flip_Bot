"""
Повідомлення про знахідки та проблеми з ринковими даними.
"""

import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from settings import EBAY_SELLING_FEES_PCT, RESALE_SHIPPING_EUR, log
from textparse import _group_label, plural
from market import estimate_resale_profit, max_buy_price
from panel import notify
from db import count_unseen_deals


async def _send_single_deal(app, w, deal_id, it, stat):
    sale_price = stat["sale_price"] or stat["median_price"]
    _, profit = estimate_resale_profit(sale_price, it["total_price"])
    buy_limit = max_buy_price(sale_price)

    warning = "\n⚠️ Низький рейтинг продавця — перевір уважно перед покупкою" if it["suspicious"] else ""
    best_offer_note = (
        "\n🎯 Можна запропонувати свою ціну (Best Offer) — реальна ціна може бути ще нижчою"
        if it["has_best_offer"] else ""
    )
    spec_note = f" · 💾 {it['spec_group']}" if it["spec_group"] != "unspecified" else ""
    price_drop_note = "\n🔻 Продавець знизив ціну" if it.get("price_dropped") else ""
    sales_line = f"{it['sales_note']}\n" if it.get("sales_note") else ""
    # Якщо ціна продажу вже рахується за справжніми продажами — застереження зайве
    estimate_note = ("" if "продан" in (stat.get("sale_source") or "")
                     else "⚠️ Ціна продажу — оцінка за поточними оголошеннями.\n")

    text = (
        f"🔥 Вигідна пропозиція: {w['label']}{price_drop_note}\n\n"
        f"{it['title']}\n"
        f"💶 Ціна з доставкою: {it['total_price']:.0f}€ (купувати варто до ~{buy_limit:.0f}€)\n"
        f"📈 Продати за: ~{sale_price:.0f}€ ({stat['sale_source']})\n"
        f"📊 Типова ціна оголошень: ~{stat['median_price']:.0f}€\n"
        f"{sales_line}"
        f"💰 Орієнтовний прибуток: ~{profit:.0f}€ "
        f"(після комісії eBay ~{EBAY_SELLING_FEES_PCT}% і доставки ~{RESALE_SHIPPING_EUR}€)\n"
        f"🏷️ Стан: {it.get('condition') or 'н/д'}{spec_note}{warning}{best_offer_note}\n"
        f"{estimate_note}"
        f"🔗 {it['url']}"
    )
    buttons = [
        [
            InlineKeyboardButton("✅ Куплено", callback_data=f"buy:{deal_id}"),
            InlineKeyboardButton("❌ Пропущено", callback_data=f"skip:{deal_id}"),
        ],
        [
            InlineKeyboardButton("🙈 Сховати", callback_data=f"hided:{deal_id}"),
            InlineKeyboardButton("❌ Інший товар", callback_data=f"rejd:{deal_id}"),
        ],
        [InlineKeyboardButton("◀️ Меню", callback_data="menu:home")],
    ]
    await notify(app, w["chat_id"], text, reply_markup=InlineKeyboardMarkup(buttons))


async def _send_grouped_deals(app, w, new_deals):
    lines = [f"🔥 Знайдено {plural(len(new_deals), 'вигідну пропозицію', 'вигідні пропозиції', 'вигідних пропозицій')}: {w['label']}\n"]
    reject_buttons = []
    for n, (deal_id, it, stat) in enumerate(new_deals, 1):
        sale_price = stat["sale_price"] or stat["median_price"]
        _, profit = estimate_resale_profit(sale_price, it["total_price"])
        warning = " ⚠️" if it["suspicious"] else ""
        offer_mark = " 🎯" if it["has_best_offer"] else ""
        drop_mark = " 🔻" if it.get("price_dropped") else ""
        spec_note = f" · 💾 {it['spec_group']}" if it["spec_group"] != "unspecified" else ""
        reject_buttons.append([
            InlineKeyboardButton(f"🙈 #{n} сховати", callback_data=f"hided:{deal_id}"),
            InlineKeyboardButton(f"❌ #{n} інший товар", callback_data=f"rejd:{deal_id}"),
        ])
        lines.append(
            f"{n}. {it['title'][:120]}\n"
            f"💰 {it['total_price']:.0f}€ → продаж ~{sale_price:.0f}€, прибуток ~{profit:.0f}€"
            f"{spec_note}{warning}{offer_mark}{drop_mark}\n"
            + (f"{it['sales_note']}\n" if it.get("sales_note") else "")
            + f"🔗 {it['url']}"
        )
    rows = list(reject_buttons)
    rows.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:home")])
    await notify(app, w["chat_id"], "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


DEAL_RENOTIFY_MINUTES = 60   # поки попередні не переглянуті — нове сповіщення не частіше


async def _notify_new_deals(app, chat_id, found):
    """
    Одне коротке сповіщення на всю пачку нових вигідних пропозицій (замість
    окремого повідомлення на кожну). Самі пропозиції — у «🔥 Вигідні пропозиції».
    Якщо попередні ще не переглянуті, а сповіщення було нещодавно, — лише
    тихо оновлюємо його текст (без нового дзвіночка).
    found: [(deal_id, item, stat, watch)], найкращу покажемо першою.
    """
    if not found:
        return
    best = max(found, key=lambda f: estimate_resale_profit(f[2]["sale_price"] or f[2]["median_price"],
                                                           f[1]["total_price"])[1])
    _, it, stat, w = best
    _, profit = estimate_resale_profit(stat["sale_price"] or stat["median_price"], it["total_price"])
    unseen = count_unseen_deals(chat_id)
    drop = "\n🔻 Продавець знизив ціну" if len(found) == 1 and it.get("price_dropped") else ""
    if unseen <= 1:
        head = "🔥 Нова вигідна пропозиція"
    else:
        head = f"🔥 Нові вигідні пропозиції: {unseen}"
    text = (f"{head}\n\n{'Найвигідніша — ' if unseen > 1 else ''}{w['label']}: "
            f"{it['total_price']:.0f}€ → прибуток ~{profit:.0f}€{drop}")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔥 Відкрити пропозиції", callback_data="deals:0")]])

    notices = app.bot_data.setdefault("deal_notice", {})
    prev = notices.get(chat_id)
    had_unseen_before = unseen > len(found)
    if prev and had_unseen_before and time.time() - prev["sent_at"] < DEAL_RENOTIFY_MINUTES * 60:
        try:
            await app.bot.edit_message_text(text, chat_id=chat_id, message_id=prev["message_id"], reply_markup=markup)
            app.bot_data.setdefault("chats_to_repost_panel", set()).add(chat_id)  # оновити лічильник у меню
            return
        except Exception as e:
            log.debug("Не вдалося оновити сповіщення про пропозиції: %s", e)
    try:
        msg = await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    except Exception as e:
        log.warning("Не вдалося надіслати сповіщення про пропозиції в чат %s: %s", chat_id, e)
        return
    if prev:
        try:
            await app.bot.delete_message(chat_id=chat_id, message_id=prev["message_id"])
        except Exception:
            pass
    notices[chat_id] = {"message_id": getattr(msg, "message_id", None), "sent_at": time.time()}
    app.bot_data.setdefault("chats_to_repost_panel", set()).add(chat_id)


async def _notify_price_drops(app, w, drops):
    """📉 Типова ціна групи за тиждень помітно впала — попередження перед покупкою."""
    lines = [f"📉 Ціни на «{w['label']}» падають\n"]
    for cond, spec, old, new, pct in drops:
        lines.append(f"• {_group_label(cond, spec)}: {old:.0f}€ → {new:.0f}€ за тиждень ({pct:.0f}%)")
    lines.append("\nКупуй обережно: поки товар дійде і буде перепроданий, ціна може впасти ще. "
                 "«Купувати до» бот уже знизив відповідно до нових цін.")
    buttons = [[InlineKeyboardButton("📊 Відкрити товар", callback_data=f"watch_details:{w['id']}")]]
    try:
        await notify(app, w["chat_id"], "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        log.warning("Не вдалося надіслати попередження про падіння цін для watch #%s: %s", w["id"], e)


async def _notify_median_ready(app, w, new_stats):
    """Сповіщає, щойно для товару вперше порахувалась статистика групи."""
    lines = [f"📊 Ринок для «{w['label']}» проаналізовано — тепер шукаю вигідні пропозиції!\n"]
    for s in new_stats:
        sale_price = s["sale_price"] or s["median_price"]
        buy_limit = max_buy_price(sale_price)
        lines.append(
            f"• {_group_label(s['cond_group'], s['spec_group'])}: продати ~{sale_price:.0f}€, "
            f"купувати до ~{buy_limit:.0f}€ ({plural(s['sample_size'], 'оголошення', 'оголошення', 'оголошень')})"
        )
    try:
        await notify(app, w["chat_id"], "\n".join(lines))
    except Exception as e:
        log.warning("Не вдалося надіслати сповіщення про ринок для watch #%s: %s", w["id"], e)


async def _notify_median_problem(app, w, reason):
    text = (
        f"⚠️ Не вдалося порахувати ціни для «{w['label']}».\n\n"
        f"Причина: {reason}\n\n"
        "Спробуй оновити оголошення пізніше або перевірити назву товару."
    )
    buttons = [[
        InlineKeyboardButton("📊 Відкрити товар", callback_data=f"watch_details:{w['id']}")
    ]]
    try:
        await notify(app, w["chat_id"], text, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        log.warning("Не вдалося надіслати пояснення проблеми медіани для watch #%s: %s", w["id"], e)


async def _notify_median_error(app, w, error):
    reason = str(error).strip() or error.__class__.__name__
    await _notify_median_problem(
        app,
        w,
        f"внутрішня помилка під час запиту або обробки даних: {reason}",
    )