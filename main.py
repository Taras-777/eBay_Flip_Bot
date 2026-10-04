"""
Точка входу: створення бота, реєстрація обробників і запуск.
"""

import warnings

import config
from telegram.warnings import PTBUserWarning

# Діалоги свідомо працюють з per_message=False (у них є і кнопки, і введення
# тексту) — попередження PTB про це лише інформаційне, ховаємо його.
warnings.filterwarnings("ignore", message=r".*per_message=False", category=PTBUserWarning)

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, PersistenceInput, PicklePersistence, filters

import settings
from settings import log
from version import get_version
from db import init_db
from panel import menu_home_callback, refresh_usage_callback
from account import (
    EBAY_CODE,
    ebay_account_cancel,
    ebay_account_code,
    ebay_account_disconnect,
    sold_only_callback,
    ebay_account_interrupt,
    ebay_account_start,
    backup_send_callback,
)
from action_log import TimedApplication, slowlog_callback
from concurrency import DialogSafeUpdateProcessor
from screen_notices import notices_callback
from screen_unknown import unknown_callback
from access import access_decision_callback, cmd_approve, cmd_pending, cmd_revoke, cmd_users, cmd_userstats, delete_user_callback
from handlers import (
    ASK_ASPECT,
    ASK_CATEGORY,
    ASK_CUSTOM_MIN_PRICE,
    ASK_MIN_PRICE_CHOICE,
    ASK_QUERY,
    EDIT_VALUE,
    addwatch_aspect_choice,
    addwatch_cancel,
    CHECK_LINK,
    check_cancel,
    check_listing_interrupt,
    check_listing_start,
    check_listing_text,
    addwatch_category_choice,
    addwatch_shared_choice,
    addwatch_custom_min_price,
    addwatch_got_query,
    addwatch_menu_interrupt,
    addwatch_min_price_choice,
    addwatch_start,
    all_configs_callback,
    sales_callback,
    sales_reject_callback,
    sales_check_now_callback,
    deals_callback,
    min_profit_callback,
    deal_inbox_action_callback,
    deals_clear_callback,
    refresh_all_callback,
    refresh_stop_callback,
    change_category_callback,
    cmd_list,
    cmd_menu,
    cmd_start,
    cmd_stats,
    config_listings_callback,
    deal_action_callback,
    delwatch_ask_callback,
    discover_add_callback,
    discover_hide_callback,
    discover_hidden_callback,
    discover_callback,
    discover_refresh_callback,
    delwatch_yes_callback,
    edit_cancel,
    edit_interrupt,
    edit_menu_callback,
    edit_value_button,
    edit_value_start,
    edit_value_text,
    recalculate_median_callback,
    listing_page_callback,
    reject_deal_callback,
    reject_listing_callback,
    unlearn_word_callback,
    required_aspect_callback,
    save_aspects_callback,
    set_category_callback,
    set_required_aspect_callback,
    toggle_aspect_callback,
    view_listings_callback,
    listing_old_callback,
    watch_details_callback,
    learned_words_callback,
)
from screen_undo import undo_callback
from screen_markdowns import drop_pct_callback, markdown_action_callback, markdowns_callback
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
    update_processor = DialogSafeUpdateProcessor(settings.CONCURRENT_UPDATES)
    app = (
        Application.builder()
        .application_class(TimedApplication)   # 🐢 журнал повільних дій
        .token(config.TELEGRAM_BOT_TOKEN)
        # Кілька натискань обробляються одночасно: повільна дія (оновлення цін, перевірка
        # оголошення) більше не змушує інші кнопки чекати в черзі
        .concurrent_updates(update_processor)
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
                CallbackQueryHandler(addwatch_shared_choice, pattern="^shr:"),
                menu_interrupt,
            ],
            ASK_ASPECT: [
                CallbackQueryHandler(addwatch_aspect_choice, pattern="^nasp:"),
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
        },
        fallbacks=[
            CommandHandler("cancel", addwatch_cancel),
            menu_interrupt,
        ],
    )

    edit_conv = ConversationHandler(
        name="edit_conv",
        persistent=True,
        entry_points=[CallbackQueryHandler(edit_value_start, pattern="^edmin:")],
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

    check_conv = ConversationHandler(
        name="check_conv",
        persistent=True,
        allow_reentry=True,
        entry_points=[CallbackQueryHandler(check_listing_start, pattern="^chkl:")],
        states={
            CHECK_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_listing_text),
                CallbackQueryHandler(check_listing_interrupt, pattern="^(watch_details:|menu:)"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", check_cancel),
            CallbackQueryHandler(check_listing_interrupt, pattern="^(watch_details:|menu:)"),
        ],
    )

    account_conv = ConversationHandler(
        name="account_conv",
        persistent=True,
        allow_reentry=True,
        entry_points=[CallbackQueryHandler(ebay_account_start, pattern="^(menu:ebay_account|eacc:connect)$")],
        states={
            EBAY_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ebay_account_code),
                CallbackQueryHandler(ebay_account_interrupt, pattern="^menu:"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", ebay_account_cancel),
            CallbackQueryHandler(ebay_account_interrupt, pattern="^menu:"),
        ],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(addwatch_conv)
    app.add_handler(edit_conv)
    app.add_handler(check_conv)
    app.add_handler(account_conv)
    update_processor.conversations = [addwatch_conv, edit_conv, check_conv, account_conv]
    app.add_handler(CallbackQueryHandler(ebay_account_disconnect, pattern="^eacc:disconnect$"))
    app.add_handler(CallbackQueryHandler(sold_only_callback, pattern="^soldonly$"))
    app.add_handler(CallbackQueryHandler(slowlog_callback, pattern="^slowlog(:clear)?$"))
    app.add_handler(CallbackQueryHandler(unknown_callback, pattern="^unk[a-z]*:"))
    app.add_handler(CallbackQueryHandler(notices_callback, pattern="^ntc:(\\d+|clear)$"))
    app.add_handler(CallbackQueryHandler(backup_send_callback, pattern="^backup:send$"))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # Команди власника (керування доступом)
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("userstats", cmd_userstats))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("approve", cmd_approve))

    app.add_handler(CallbackQueryHandler(deal_action_callback, pattern="^(buy|skip):"))
    app.add_handler(CallbackQueryHandler(access_decision_callback, pattern="^access:"))
    app.add_handler(CallbackQueryHandler(delete_user_callback, pattern="^udel(ok)?:"))
    # «🚫 Не той товар», «🙈 Сховати» і скасування вивчених слів
    app.add_handler(CallbackQueryHandler(reject_deal_callback, pattern="^(rejd|hided):"))
    app.add_handler(CallbackQueryHandler(reject_listing_callback, pattern="^(rejl|hidel):"))
    app.add_handler(CallbackQueryHandler(listing_page_callback, pattern="^lpage:"))
    app.add_handler(CallbackQueryHandler(unlearn_word_callback, pattern="^unlw:"))
    # Кнопки головного меню (коли жоден діалог не активний)
    app.add_handler(CallbackQueryHandler(undo_callback, pattern="^undo:"))
    app.add_handler(CallbackQueryHandler(markdowns_callback, pattern="^mkd:"))
    app.add_handler(CallbackQueryHandler(markdown_action_callback, pattern="^mact:"))
    app.add_handler(CallbackQueryHandler(drop_pct_callback, pattern="^mdpct:"))
    app.add_handler(CallbackQueryHandler(menu_home_callback, pattern="^menu:home$"))
    app.add_handler(CallbackQueryHandler(refresh_usage_callback, pattern="^menu:refresh_usage$"))
    app.add_handler(CallbackQueryHandler(refresh_all_callback, pattern="^menu:refresh_prices$"))
    app.add_handler(CallbackQueryHandler(refresh_stop_callback, pattern="^refresh_stop$"))
    app.add_handler(CallbackQueryHandler(discover_callback, pattern="^(menu:discover|dpage:\\d+)$"))
    app.add_handler(CallbackQueryHandler(discover_add_callback, pattern="^dadd:"))
    app.add_handler(CallbackQueryHandler(discover_hide_callback, pattern="^dhide:"))
    app.add_handler(CallbackQueryHandler(discover_hidden_callback, pattern="^(dhidden|dunhide:)"))
    app.add_handler(CallbackQueryHandler(discover_refresh_callback, pattern="^drefresh$"))
    app.add_handler(CallbackQueryHandler(cmd_list, pattern="^menu:list$"))
    app.add_handler(CallbackQueryHandler(cmd_stats, pattern="^(menu:stats|stats:d\\d+)$"))
    app.add_handler(CallbackQueryHandler(watch_details_callback, pattern="^watch_details:"))
    app.add_handler(CallbackQueryHandler(learned_words_callback, pattern="^(lwords|lwdel):"))
    app.add_handler(CallbackQueryHandler(cmd_pending, pattern="^menu:pending$"))
    app.add_handler(CallbackQueryHandler(cmd_users, pattern="^menu:users$"))
    app.add_handler(CallbackQueryHandler(recalculate_median_callback, pattern="^recalc_median:"))
    app.add_handler(CallbackQueryHandler(view_listings_callback, pattern="^view_listings:"))
    app.add_handler(CallbackQueryHandler(listing_old_callback, pattern="^lold:"))
    app.add_handler(CallbackQueryHandler(change_category_callback, pattern="^chcat:"))
    app.add_handler(CallbackQueryHandler(all_configs_callback, pattern="^configs:"))
    app.add_handler(CallbackQueryHandler(sales_callback, pattern="^sales:"))
    app.add_handler(CallbackQueryHandler(sales_reject_callback, pattern="^srej:"))
    app.add_handler(CallbackQueryHandler(sales_check_now_callback, pattern="^schk:"))
    app.add_handler(CallbackQueryHandler(deals_callback, pattern="^deals:"))
    app.add_handler(CallbackQueryHandler(min_profit_callback, pattern="^mprof:"))
    app.add_handler(CallbackQueryHandler(deal_inbox_action_callback, pattern="^dact:"))
    app.add_handler(CallbackQueryHandler(deals_clear_callback, pattern="^dclear$"))
    app.add_handler(CallbackQueryHandler(edit_menu_callback, pattern="^editw:"))
    app.add_handler(CallbackQueryHandler(config_listings_callback, pattern="^cfgl:"))
    app.add_handler(CallbackQueryHandler(required_aspect_callback, pattern="^reqasp:"))
    app.add_handler(CallbackQueryHandler(set_required_aspect_callback, pattern="^setasp:"))
    app.add_handler(CallbackQueryHandler(toggle_aspect_callback, pattern="^tglasp:"))
    app.add_handler(CallbackQueryHandler(save_aspects_callback, pattern="^saveasp:"))
    app.add_handler(CallbackQueryHandler(set_category_callback, pattern="^setcat:"))
    # Підтвердження видалення
    app.add_handler(CallbackQueryHandler(delwatch_ask_callback, pattern="^delwatch_ask:"))
    app.add_handler(CallbackQueryHandler(delwatch_yes_callback, pattern="^delwatch_yes:"))

    app.add_error_handler(error_handler)

    log.info("Бот запущено, версія %s", get_version())
    app.run_polling()


# Сумісність: старі скрипти й тести звертаються до main.<назва> — шукаємо
# назву в модулях бота (присвоєння main.<назва> = ... на модулі не впливає).
import access, db, ebay_api, handlers, market, notifications, panel, scheduler, textparse  # noqa: E402

_MODULES = (settings, textparse, db, ebay_api, market, panel, access, notifications, handlers, scheduler)


def __getattr__(name):
    for module in _MODULES:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module 'main' has no attribute {name!r}")


if __name__ == "__main__":
    main()
