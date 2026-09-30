"""
«↩️ Скасувати» — повернути останню дію як було.

Кожна дія, яку можна скасувати, записує в базу (undo_actions), що саме вона
змінила. Кнопка «↩️ Скасувати: …» з'являється на екрані одразу після дії і ще
UNDO_MENU_MINUTES висить у головному меню; скасувати можна протягом
UNDO_KEEP_HOURS (стільки ж зберігається історія видаленого товару).

Що скасовується:
  * 🗑️ видалення товару (з усією історією цін і продажів);
  * ❌ Інший товар / 🙈 Сховати — в оголошеннях, пропозиціях і продажах
    (разом зі словами, які бот при цьому вивчив);
  * ✅ Куплено і 🧹 Очистити список у «🔥 Вигідні пропозиції»;
  * 🗑 прибране вивчене слово;
  * 🗑 видалення користувача (власник).
"""

import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from settings import UNDO_KEEP_HOURS, UNDO_MENU_MINUTES
from db import (
    add_undo,
    forget_learned_words,
    get_watch,
    latest_undo,
    mark_market_stale,
    restore_obs_rows,
    restore_user,
    restore_watch,
    set_deals_status,
    set_learned_word,
    unreject_item,
)
from learning import _set_exclude_words

OFFER_KEY = "undo_offer"


def short(text, limit=35):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def record(context, chat_id, kind, label, **payload):
    """Записує дію й пропонує «↩️ Скасувати» на наступному екрані. Повертає id."""
    undo_id = add_undo(chat_id, kind, label, payload)
    if context is not None:
        context.user_data[OFFER_KEY] = {"id": undo_id, "label": label}
    return undo_id


def undo_button(undo_id, label):
    return InlineKeyboardButton(short(f"↩️ Скасувати: {label}", 60), callback_data=f"undo:{undo_id}")


def attach_offer(context, reply_markup):
    """Додає «↩️ Скасувати» першим рядком екрана одразу після дії (один раз)."""
    offer = context.user_data.get(OFFER_KEY) if context is not None else None
    if not isinstance(offer, dict) or not isinstance(reply_markup, InlineKeyboardMarkup):
        return reply_markup
    context.user_data.pop(OFFER_KEY, None)
    rows = [list(r) for r in reply_markup.inline_keyboard]
    if any(b.callback_data == f"undo:{offer['id']}" for r in rows for b in r):
        return reply_markup   # головне меню вже показує цю кнопку
    return InlineKeyboardMarkup([[undo_button(offer["id"], offer["label"])]] + rows)


def menu_offer(chat_id):
    """Остання нескасована дія за UNDO_MENU_MINUTES — для головного меню."""
    return latest_undo(chat_id, int(time.time()) - UNDO_MENU_MINUTES * 60)


def perform(action):
    """Повертає (True, примітка) або (False, причина)."""
    if action["created_at"] < time.time() - UNDO_KEEP_HOURS * 3600:
        return False, f"⌛ Минуло понад {UNDO_KEEP_HOURS} год — цю дію вже не повернути."
    kind, p, chat_id = action["kind"], action["payload"], action["chat_id"]

    if kind == "watch_delete":
        if not restore_watch(p["watch_id"], chat_id):
            return False, "Історію цього товару вже прибрано — повернути не вийде."
        return True, "↩️ Товар повернуто разом з історією цін і продажів. Ринок оновиться за кілька хвилин."

    if kind == "user_delete":
        restore_user(p["user"], p["watch_ids"])
        return True, "↩️ Користувача повернуто — доступ і його товари знову працюють."

    if kind == "deal_status":
        set_deals_status(p["deal_ids"], p["status"])
        n = len(p["deal_ids"])
        return True, ("↩️ Пропозицію повернуто в список." if n == 1
                      else f"↩️ Повернуто пропозицій: {n}.")

    watch = get_watch(p["watch_id"], chat_id)
    if watch is None:
        return False, "Цей товар уже видалено."

    if kind == "reject":
        unreject_item(watch["id"], p["item_id"])
        restore_obs_rows(p.get("obs") or [])
        words = p.get("words") or []
        if words:
            _set_exclude_words(watch, words_to_remove=words)
            forget_learned_words(watch["id"], words)
        if p.get("deal_id"):
            set_deals_status([p["deal_id"]], p.get("deal_status", "new"))
        if p.get("refresh_sales"):
            from market import refresh_sale_prices  # тут, щоб не було циклу імпортів
            refresh_sale_prices(watch["id"])
        mark_market_stale(watch["id"])
        note = "↩️ Оголошення повернуто — знову враховується."
        if words:
            note += " Слова " + ", ".join(f"«{w}»" for w in words) + " більше не відсіюються."
        return True, note

    if kind == "word_unlearn":
        _set_exclude_words(watch, words_to_add=[p["word"]])
        set_learned_word(watch["id"], p["word"], "excluded")
        return True, f"↩️ «{p['word']}» знову відсіюється."

    return False, "Невідома дія."
