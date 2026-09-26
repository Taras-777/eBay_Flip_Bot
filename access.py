"""
Контроль доступу: власник, схвалення користувачів, команди власника.
"""

import config
import functools
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from settings import is_owner, log
from db import (
    get_deal_stats,
    get_user_row,
    list_users,
    list_watches,
    set_user_status,
    upsert_user_request,
)
from panel import _ack_callback, back_to_menu_keyboard, refresh_owner_menu, show_panel


async def _notify_owner_new_request(bot, user):
    if config.OWNER_TELEGRAM_ID == 0:
        return
    name = user.username and f"@{user.username}" or user.first_name or str(user.id)
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Дозволити", callback_data=f"access:approve:{user.id}"),
            InlineKeyboardButton("⛔ Відхилити", callback_data=f"access:deny:{user.id}"),
        ]]
    )
    await bot.send_message(
        chat_id=config.OWNER_TELEGRAM_ID,
        text=f"🔔 Новий запит на доступ до бота\n👤 {name} (ID: {user.id})",
        reply_markup=keyboard,
    )
    await refresh_owner_menu(bot)


def require_access(handler_func):
    """
    Декоратор для команд/колбеків, що доступні лише власнику або
    схваленим користувачам. Власник — повний доступ автоматично.
    Новий користувач — запит іде власнику з кнопками схвалення.
    """
    @functools.wraps(handler_func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _ack_callback(update)
        user = update.effective_user
        if is_owner(user.id):
            return await handler_func(update, context)

        row = get_user_row(user.id)
        status = row["status"] if row else None

        if status == "approved":
            return await handler_func(update, context)

        if status == "pending":
            await update.effective_message.reply_text(
                "⏳ Твій запит на доступ ще розглядається власником бота."
            )
            return

        if status == "denied":
            await update.effective_message.reply_text(
                "⛔ Власник бота вже відхилив твій попередній запит на доступ."
            )
            return

        # немає запису взагалі — це справді новий запит
        upsert_user_request(
            user.id, update.effective_chat.id, user.username, user.first_name
        )
        await _notify_owner_new_request(context.bot, user)
        await update.effective_message.reply_text(
            "🔒 Доступ до цього бота обмежений.\n"
            "Твій запит надіслано власнику — очікуй підтвердження."
        )
    return wrapper


def owner_only(handler_func):
    @functools.wraps(handler_func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _ack_callback(update)
        if not is_owner(update.effective_user.id):
            await update.effective_message.reply_text("⛔ Ця команда доступна лише власнику бота.")
            return
        return await handler_func(update, context)
    return wrapper


async def access_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_cb = update.callback_query
    if not is_owner(update.effective_user.id):
        await query_cb.answer("⛔ Лише власник може приймати це рішення.", show_alert=True)
        return
    await query_cb.answer()

    _, action, user_id_str = query_cb.data.split(":")
    target_id = int(user_id_str)
    row = get_user_row(target_id)
    if not row:
        await query_cb.edit_message_text("Запит уже неактуальний.")
        return

    if action == "approve":
        set_user_status(target_id, "approved")
        await query_cb.edit_message_text(f"{query_cb.message.text}\n\n✅ Доступ надано.")
        try:
            await context.bot.send_message(
                chat_id=row["chat_id"],
                text="✅ Твій доступ до бота підтверджено! Напиши /start, щоб почати.",
            )
        except Exception as e:
            log.warning("Не вдалося сповістити користувача %s: %s", target_id, e)
    else:
        set_user_status(target_id, "denied")
        await query_cb.edit_message_text(f"{query_cb.message.text}\n\n⛔ Доступ відхилено.")
        try:
            await context.bot.send_message(
                chat_id=row["chat_id"],
                text="⛔ Власник бота відхилив твій запит на доступ.",
            )
        except Exception as e:
            log.warning("Не вдалося сповістити користувача %s: %s", target_id, e)

    await refresh_owner_menu(context.bot)


# ============================================================
# КОМАНДИ ВЛАСНИКА: КЕРУВАННЯ ДОСТУПОМ І СТАТИСТИКА КОРИСТУВАЧІВ
# ============================================================

@owner_only
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = list_users(status="pending")
    if not pending:
        await show_panel(update, context, "Немає запитів, що очікують рішення.", reply_markup=back_to_menu_keyboard())
        return
    for u in pending:
        name = u["username"] and f"@{u['username']}" or u["first_name"] or str(u["user_id"])
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Дозволити", callback_data=f"access:approve:{u['user_id']}"),
                InlineKeyboardButton("⛔ Відхилити", callback_data=f"access:deny:{u['user_id']}"),
            ]]
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"👤 {name} (ID: {u['user_id']})",
            reply_markup=keyboard,
        )


@owner_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = list_users(status="approved")
    if not users:
        await show_panel(update, context, "Ще немає жодного схваленого користувача.", reply_markup=back_to_menu_keyboard())
        return
    lines = ["👥 Користувачі з доступом:\n"]
    for u in users:
        name = u["username"] and f"@{u['username']}" or u["first_name"] or str(u["user_id"])
        watch_count = len(list_watches(chat_id=u["chat_id"]))
        lines.append(f"👤 {name} — ID {u['user_id']} · 📦 {watch_count} відстежень")
    lines.append("\nДеталі по користувачу: /userstats <id>")
    await show_panel(update, context, "\n".join(lines), reply_markup=back_to_menu_keyboard())


@owner_only
async def cmd_userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Формат: /userstats <telegram_id>\nСписок id — /users")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом.")
        return

    row = get_user_row(target_id)
    if not row or row["status"] != "approved":
        await update.message.reply_text("Такого схваленого користувача немає.")
        return

    name = row["username"] and f"@{row['username']}" or row["first_name"] or str(target_id)
    watches = list_watches(chat_id=row["chat_id"])
    deal_stats = get_deal_stats(row["chat_id"])

    lines = [f"👤 {name} (ID: {target_id})\n"]
    if watches:
        lines.append("📦 Відстеження:")
        for w in watches:
            lines.append(f"  #{w['id']} {w['label']}")
    else:
        lines.append("📦 Немає активних відстежень.")

    lines.append(
        f"\n📊 Знахідки: ✅ куплено {deal_stats.get('bought', 0)} · "
        f"❌ пропущено {deal_stats.get('skipped', 0)} · 🆕 нових {deal_stats.get('new', 0)}"
    )
    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Формат: /revoke <telegram_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом.")
        return
    set_user_status(target_id, "denied")
    await update.message.reply_text(f"⛔ Доступ для {target_id} забрано.")


@owner_only
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручне схвалення — зокрема, щоб передумати щодо раніше
    відхиленого (denied) користувача."""
    if not context.args:
        await update.message.reply_text("Формат: /approve <telegram_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом.")
        return
    row = get_user_row(target_id)
    if not row:
        await update.message.reply_text("Цей користувач ще не звертався до бота.")
        return
    set_user_status(target_id, "approved")
    await update.message.reply_text(f"✅ Доступ для {target_id} надано.")
    try:
        await context.bot.send_message(
            chat_id=row["chat_id"],
            text="✅ Твій доступ до бота підтверджено! Напиши /start, щоб почати.",
        )
    except Exception as e:
        log.warning("Не вдалося сповістити користувача %s: %s", target_id, e)