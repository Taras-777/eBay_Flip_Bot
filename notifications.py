"""
Повідомлення про знахідки та проблеми з ринковими даними.
"""

import asyncio
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from settings import log
from textparse import _group_label, plural
from market import estimate_resale_profit, max_buy_price
from panel import refresh_menu_in_place
from db import add_notice, count_unseen_deals


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


async def _to_inbox(app, chat_id, kind, text, watch_id=None):
    """Службове повідомлення — не окремим повідомленням у чаті, а в «🔔 Повідомлення»
    (кнопка з'являється в головному меню, коли там щось є)."""
    try:
        await asyncio.to_thread(add_notice, chat_id, kind, text, watch_id)
        await refresh_menu_in_place(app, chat_id)
    except Exception as e:
        log.warning("Не вдалося зберегти повідомлення для чату %s: %s", chat_id, e)


async def _notify_price_drops(app, w, drops):
    """📉 Типова ціна групи за тиждень помітно впала — попередження перед покупкою."""
    lines = [f"📉 Ціни на «{w['label']}» падають"]
    for cond, spec, old, new, pct in drops:
        lines.append(f"• {_group_label(cond, spec)}: {old:.0f}€ → {new:.0f}€ за тиждень ({pct:.0f}%)")
    lines.append("Купуй обережно: поки товар дійде і буде перепроданий, ціна може впасти ще. "
                 "«Купувати до» бот уже знизив відповідно до нових цін.")
    await _to_inbox(app, w["chat_id"], "price_drop", "\n".join(lines), w["id"])


async def _notify_median_ready(app, w, new_stats):
    """Щойно для групи товару вперше порахувалась статистика."""
    lines = [f"📊 Ринок для «{w['label']}» проаналізовано — тепер шукаю вигідні пропозиції!"]
    for s in new_stats:
        sale_price = s["sale_price"] or s["median_price"]
        buy_limit = max_buy_price(sale_price)
        lines.append(
            f"• {_group_label(s['cond_group'], s['spec_group'])}: продати ~{sale_price:.0f}€, "
            f"купувати до ~{buy_limit:.0f}€ ({plural(s['sample_size'], 'оголошення', 'оголошення', 'оголошень')})"
        )
    await _to_inbox(app, w["chat_id"], "market_ready", "\n".join(lines), w["id"])


async def _notify_median_problem(app, w, reason):
    text = (f"⚠️ Не вдалося порахувати ціни для «{w['label']}».\n"
            f"Причина: {reason}\n"
            "Спробуй оновити оголошення пізніше або перевірити назву товару.")
    await _to_inbox(app, w["chat_id"], "problem", text, w["id"])


async def _notify_median_error(app, w, error):
    reason = str(error).strip() or error.__class__.__name__
    await _notify_median_problem(
        app,
        w,
        f"внутрішня помилка під час запиту або обробки даних: {reason}",
    )
