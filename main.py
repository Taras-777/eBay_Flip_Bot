"""
Точка входу: створення бота, реєстрація обробників і запуск.
"""

import config
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, PersistenceInput, PicklePersistence, filters

import settings
from settings import log
from db import init_db
from panel import menu_home_callback
from access import access_decision_callback, cmd_approve, cmd_pending, cmd_revoke, cmd_users, cmd_userstats
from handlers import (
    ASK_CATEGORY,
    ASK_CUSTOM_MIN_PRICE,
    ASK_CUSTOM_THRESHOLD,
    ASK_DELETE_ID,
    ASK_MIN_PRICE_CHOICE,
    ASK_QUERY,
    ASK_THRESHOLD_CHOICE,
    EDIT_VALUE,
    addwatch_cancel,
    addwatch_category_choice,
    addwatch_custom_min_price,
    addwatch_custom_threshold,
    addwatch_got_query,
    addwatch_menu_interrupt,
    addwatch_min_price_choice,
    addwatch_start,
    addwatch_threshold_choice,
    all_configs_callback,
    change_category_callback,
    cmd_list,
    cmd_menu,
    cmd_remove,
    cmd_requirespec,
    cmd_setconditions,
    cmd_setexclude,
    cmd_setminprice,
    cmd_setthreshold,
    cmd_start,
    cmd_stats,
    config_listings_callback,
    deal_action_callback,
    delete_cancel,
    delete_menu_interrupt,
    delwatch_prompt_callback,
    delwatch_yes_callback,
    edit_cancel,
    edit_interrupt,
    edit_menu_callback,
    edit_value_button,
    edit_value_start,
    edit_value_text,
    got_delete_id,
    recalculate_median_callback,
    required_aspect_callback,
    save_aspects_callback,
    set_category_callback,
    set_required_aspect_callback,
    toggle_aspect_callback,
    view_listings_callback,
    watch_details_callback,
)
from scheduler import error_handler, post_init, post_shutdown


# ============================================================
# ЗАПУСК
# ============================================================

def main():
    init_db()
    # Зберігає стан користувачів (де панель меню, незавершені діалоги) у файл,
    # щоб після перезапуску бот продовжив з того ж місця. bot_data не
    # зберігаємо — там фонова задача, яку не можна записати у файл.
    persistence = PicklePersistence(
        filepath=settings.STATE_FILE,
        store_data=PersistenceInput(bot_data=False, chat_data=False, user_data=True, callback_data=False),
    )
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    menu_interrupt = CallbackQueryHandler(addwatch_menu_interrupt, pattern="^menu:")
    addwatch_conv = ConversationHandler(
        name="addwatch_conv",
        persistent=True,
        entry_points=[
            CommandHandler("addwatch", addwatch_start),
            CallbackQueryHandler(addwatch_start, pattern="^menu:addwatch$"),
        ],
        states={
            ASK_QUERY: [
                menu_interrupt,
                MessageHandler(filters.TEXT & ~filters.COMMAND, addwatch_got_query),
            ],
            ASK_CATEGORY: [
                CallbackQueryHandler(addwatch_category_choice, pattern="^cat:"),
                menu_interrupt,
            ],
            ASK_MIN_PRICE_CHOICE: [
                CallbackQueryHandler(addwatch_min_price_choice, pattern="^minp:"),
                menu_interrupt,
            ],
            ASK_CUSTOM_MIN_PRICE: [
                menu_interrupt,
                MessageHandler(filters.TEXT & ~filters.COMMAND, addwatch_custom_min_price),
            ],
            ASK_THRESHOLD_CHOICE: [
                CallbackQueryHandler(addwatch_threshold_choice, pattern="^use_"),
                menu_interrupt,
            ],
            ASK_CUSTOM_THRESHOLD: [
                menu_interrupt,
                MessageHandler(filters.TEXT & ~filters.COMMAND, addwatch_custom_threshold),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", addwatch_cancel),
            menu_interrupt,
        ],
    )

    delete_conv = ConversationHandler(
        name="delete_conv",
        persistent=True,
        entry_points=[
            CallbackQueryHandler(delwatch_prompt_callback, pattern="^delwatch_prompt$"),
        ],
        states={
            ASK_DELETE_ID: [
                CallbackQueryHandler(delete_menu_interrupt, pattern="^menu:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_delete_id),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", delete_cancel),
            CallbackQueryHandler(delete_menu_interrupt, pattern="^menu:"),
        ],
    )

    edit_conv = ConversationHandler(
        name="edit_conv",
        persistent=True,
        entry_points=[CallbackQueryHandler(edit_value_start, pattern="^(edpct|edmin):")],
        states={
            EDIT_VALUE: [
                CallbackQueryHandler(edit_value_button, pattern="^edval:"),
                CallbackQueryHandler(edit_interrupt, pattern="^(editw:|watch_details:|menu:)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value_text),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", edit_cancel),
            CallbackQueryHandler(edit_interrupt, pattern="^(editw:|watch_details:|menu:)"),
        ],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(addwatch_conv)
    app.add_handler(delete_conv)
    app.add_handler(edit_conv)
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("setthreshold", cmd_setthreshold))
    app.add_handler(CommandHandler("setminprice", cmd_setminprice))
    app.add_handler(CommandHandler("setexclude", cmd_setexclude))
    app.add_handler(CommandHandler("requirespec", cmd_requirespec))
    app.add_handler(CommandHandler("setconditions", cmd_setconditions))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # Команди власника (керування доступом)
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("userstats", cmd_userstats))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("approve", cmd_approve))

    app.add_handler(CallbackQueryHandler(deal_action_callback, pattern="^(buy|skip):"))
    app.add_handler(CallbackQueryHandler(access_decision_callback, pattern="^access:"))
    # Кнопки головного меню (коли жоден діалог не активний)
    app.add_handler(CallbackQueryHandler(menu_home_callback, pattern="^menu:home$"))
    app.add_handler(CallbackQueryHandler(cmd_list, pattern="^menu:list$"))
    app.add_handler(CallbackQueryHandler(cmd_stats, pattern="^menu:stats$"))
    app.add_handler(CallbackQueryHandler(watch_details_callback, pattern="^watch_details:"))
    app.add_handler(CallbackQueryHandler(cmd_pending, pattern="^menu:pending$"))
    app.add_handler(CallbackQueryHandler(cmd_users, pattern="^menu:users$"))
    app.add_handler(CallbackQueryHandler(recalculate_median_callback, pattern="^recalc_median:"))
    app.add_handler(CallbackQueryHandler(view_listings_callback, pattern="^view_listings:"))
    app.add_handler(CallbackQueryHandler(change_category_callback, pattern="^chcat:"))
    app.add_handler(CallbackQueryHandler(all_configs_callback, pattern="^configs:"))
    app.add_handler(CallbackQueryHandler(edit_menu_callback, pattern="^editw:"))
    app.add_handler(CallbackQueryHandler(config_listings_callback, pattern="^cfgl:"))
    app.add_handler(CallbackQueryHandler(required_aspect_callback, pattern="^reqasp:"))
    app.add_handler(CallbackQueryHandler(set_required_aspect_callback, pattern="^setasp:"))
    app.add_handler(CallbackQueryHandler(toggle_aspect_callback, pattern="^tglasp:"))
    app.add_handler(CallbackQueryHandler(save_aspects_callback, pattern="^saveasp:"))
    app.add_handler(CallbackQueryHandler(set_category_callback, pattern="^setcat:"))
    # Підтвердження видалення
    app.add_handler(CallbackQueryHandler(delwatch_yes_callback, pattern="^delwatch_yes:"))

    app.add_error_handler(error_handler)

    log.info("Бот запущено")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()


# Сумісність: старі скрипти й тести звертаються до main.<назва> — шукаємо
# назву в модулях бота (присвоєння main.<назва> = ... на модулі не впливає).
import access, db, ebay_api, handlers, market, notifications, panel, scheduler, settings, textparse  # noqa: E402

_MODULES = (settings, textparse, db, ebay_api, market, panel, access, notifications, handlers, scheduler)


def __getattr__(name):
    for module in _MODULES:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module 'main' has no attribute {name!r}")


if __name__ == "__main__":
    main()